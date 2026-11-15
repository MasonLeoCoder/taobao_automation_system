"""生意参谋原值、分页快照和最新批次选择回归。"""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage import sycm_search as repo
from ecommerce.infrastructure.storage.mysql_migrations.schema_v28 import NEW_TABLES, build_metadata
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.tasks.runner import TaskResult
from ecommerce.modules.reports.sycm_search import SearchRankingWorkbench
from ecommerce.platforms.sycm import search_rank
from ecommerce.platforms.sycm.search_rank import parse_rows
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention


class RankingSnapshotTests(TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        metadata = sa.MetaData()
        source = build_metadata()
        for name in NEW_TABLES:
            columns = [sa.Column(col.name, sa.Integer if col.name == 'id' and name != NEW_TABLES[0] else
                                 sa.String(32) if getattr(col.type, 'collation', None) == 'ascii_bin' else col.type,
                                 primary_key=col.primary_key, nullable=col.nullable)
                       for col in source.tables[name].columns]
            sa.Table(name, metadata, *columns)
        self.tasks = sa.Table('tasks', metadata, sa.Column('id', sa.String(32), primary_key=True),
            sa.Column('name', sa.String()), sa.Column('status', sa.String()), sa.Column('message', sa.String()),
            sa.Column('account_id', sa.String(32)), sa.Column('shop_id', sa.String(32)),
            sa.Column('finished_at', sa.DateTime()), sa.Column('owner_pid', sa.Integer()),
            sa.Column('owner_process_started_at', sa.DateTime()))
        self.events = sa.Table('task_events', metadata, sa.Column('task_id', sa.String()),
            sa.Column('task_sequence', sa.Integer()), sa.Column('event_type', sa.String()), sa.Column('details', sa.JSON()))
        metadata.create_all(self.engine)

        @contextmanager
        def connection(settings, *, write=False):
            with (self.engine.begin() if write else self.engine.connect()) as conn:
                yield conn, metadata.tables

        self.enterContext(patch.object(repo, 'connection', connection))
        self.category = {'id': 42, 'full_path': '宠物 > 粘毛器'}

    def save(self, pages, actual, *, error=False, source=None):
        batch = uuid4().hex
        task_id = uuid4().hex
        ids = repo.begin(None, batch, task_id, self.category, '7d', pages, ['search'])
        if source:
            with self.engine.begin() as conn:
                conn.execute(self.tasks.insert().values(id=task_id, name='sycm.search_rank',
                    status='succeeded', finished_at=database_now(), account_id=source['account_id'], shop_id=source['shop_id']))
                conn.execute(self.events.insert().values(task_id=task_id, task_sequence=1, event_type='task_started',
                    details={'qianniu_source': source}))
        for page in range(1, actual + 1):
            repo.save_page(None, ids['search'], page, [{
                'rank': page, 'keyword': str(page), 'popularity_raw': '300 ~ 600', 'click_rate_raw': '106%',
                'payment_conversion_raw': '30% ~ 35%', 'extra_metrics': {},
            }], database_now())
        repo.finish_type(None, ids['search'], 'error' if error else 'requested', actual+1 if error else None)
        repo.finish(None, batch)
        return batch

    def test_latest_partial_with_rows_wins_and_failed_empty_remains_in_history(self):
        older = self.save(10, 10)
        newer = self.save(10, 2, error=True)
        failed = self.save(10, 0, error=True)
        data = repo.browse(None, 42, '7d')
        self.assertEqual([row['id'] for row in data['history']], [failed, newer, older])
        self.assertEqual(data['batch']['id'], newer)
        self.assertEqual((data['types']['search']['status'], len(data['entries'])), ('partial', 2))
        self.assertEqual(len(repo.browse(None, 42, '7d', older)['entries']), 10)
        self.assertEqual(repo.browse(None, 42, '7d', failed)['entries'], [])

    def test_first_page_medal_and_raw_metrics(self):
        headings = ['排名','核心词','搜索人气','点击率','支付转化率','其他指标','操作']
        cells = [{'text': value, 'medal': index == 0} for index, value in
                 enumerate(['','猫','300 ~ 600','106%','30% ~ 35%','-','搜索分析'])]
        result = parse_rows(headings, [cells], '核心词', 1)[0]
        self.assertEqual((result['rank'], result['keyword'], result['click_rate_raw']), (1, '猫', '106%'))
        self.assertEqual(result['extra_metrics'], {'其他指标': '-'})

    def test_latest_and_export_never_mix_batches_and_history_can_filter_unknown_source(self):
        historical = self.save(1, 1)
        source_a = dict(shop_id='a' * 32, account_id='b' * 32, shop_name='A店', login_name='tb123:大橘',
                        platform_shop_id='123', request_hash='request-a')
        source_b = dict(shop_id='c' * 32, account_id='d' * 32, shop_name='B店', login_name='tb456',
                        platform_shop_id='456', request_hash='request-b')
        older = self.save(2, 2, source=source_a)
        newer = self.save(1, 1, source=source_b)
        self.assertEqual(repo.browse(None, 42, '7d')['batch']['id'], newer)
        match = repo.browse(None, 42, '7d', source_shop=source_a['shop_id'])
        self.assertEqual(match['batch']['id'], older)
        self.assertEqual(match['batch']['login_name'], 'tb123:大橘')
        self.assertEqual(len(match['entries']), 2)
        self.assertEqual(repo.request_status(None, match['batch']['task_id'])['target'], source_a)
        self.assertEqual(repo.browse(None, 42, '7d', source_account='unknown')['batch']['id'], historical)
        with self.assertRaises(LookupError):
            repo.browse(None, 42, '7d', newer, source_account=source_a['account_id'])


class RankingWorkbenchTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workbench = SearchRankingWorkbench(load_settings(root=Path(directory.name)))
        self.category = {'id': 42, 'full_path': '宠物 > 粘毛器'}
        self.row = {'id': 'a' * 32, 'shop_id': 'b' * 32, 'login_name': 'tb123:大橘',
                    'shop_name': 'A店', 'platform_shop_id': '123'}
        self.module = 'ecommerce.modules.reports.sycm_search'

    def test_duplicate_request_reuses_active_and_durable_state(self):
        with (patch(f'{self.module}.selected_account', return_value=self.row),
              patch(f'{self.module}.repository.request_status', return_value=None) as stored,
              patch(f'{self.module}.Thread') as thread):
            kwargs = dict(account_id=self.row['id'], shop_id=self.row['shop_id'], request_id='c' * 32)
            args = (self.category, '宠物 > 粘毛器', '7d', 1, ['search'])
            first = self.workbench.start(*args, **kwargs)
            self.assertEqual(self.workbench.start(*args, **kwargs), first)
            thread.assert_called_once()
            self.workbench.state = None
            stored.return_value = dict(first, phase='succeeded')
            self.assertEqual(self.workbench.start(*args, **kwargs)['phase'], 'succeeded')
            with self.assertRaisesRegex(ValueError, '其他设置'):
                self.workbench.start(self.category, '宠物 > 粘毛器', '30d', 1, ['search'], **kwargs)
            thread.assert_called_once()

    def test_partial_failure_preserves_page_and_marks_login_only_when_confirmed(self):
        for error in (NeedsAttention('当前账号没有权限'), search_rank.RankingLoginRequired('千牛已显示登录页')):
            with self.subTest(error=type(error)):
                def collect(page, scope, word, requested, save):
                    save(1, [{'keyword': '已保存'}])
                    raise error

                def run(settings, name, work, **kwargs):
                    try:
                        work()
                    except NeedsAttention as problem:
                        return TaskResult('c' * 32, 'needs_attention', str(problem), 3)
                    self.fail('采集应停止')

                def expired(settings, row):
                    raise NeedsAttention('请重新登录')

                self.workbench.state = {}
                with (patch(f'{self.module}.run_task', side_effect=run),
                      patch(f'{self.module}.current_task', return_value=(None, 'c' * 32)),
                      patch(f'{self.module}.check_cooldown'),
                      patch(f'{self.module}.selected_account', return_value=self.row),
                      patch(f'{self.module}.account_settings'),
                      patch(f'{self.module}.verify_session'),
                      patch(f'{self.module}.require_login', side_effect=expired) as login,
                      patch(f'{self.module}.browser_session') as browser,
                      patch(f'{self.module}.collection_pacing'),
                      patch(f'{self.module}.repository.begin', return_value={'search': 1}),
                      patch(f'{self.module}.repository.confirm_scope'),
                      patch(f'{self.module}.repository.save_page') as saved,
                      patch(f'{self.module}.repository.finish', return_value='partial') as finish,
                      patch(f'{self.module}.search_rank.prepare', return_value=('宠物 > 粘毛器', None, None, None)),
                      patch(f'{self.module}.search_rank.collect_type', side_effect=collect)):
                    browser.return_value.__enter__.return_value = (MagicMock(), MagicMock())
                    self.workbench._run('c' * 32, self.category, '宠物 > 粘毛器', '7d', 2, ['search'],
                                        {'shop_id': self.row['shop_id'], 'account_id': self.row['id']})
                    saved.assert_called_once()
                    finish.assert_called_once()
                    self.assertTrue(self.workbench.state['has_batch'])
                    self.assertEqual(self.workbench.state['phase'], 'needs_attention')
                    self.assertEqual(login.called, isinstance(error, search_rank.RankingLoginRequired))
