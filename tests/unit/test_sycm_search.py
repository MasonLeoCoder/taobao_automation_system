"""生意参谋原值、分页快照和最新批次选择回归。"""
from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage import sycm_search as repo
from ecommerce.infrastructure.storage.mysql_migrations.schema_v28 import NEW_TABLES, build_metadata
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.platforms.sycm.search_rank import parse_rows


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
        metadata.create_all(self.engine)

        @contextmanager
        def connection(settings, *, write=False):
            with (self.engine.begin() if write else self.engine.connect()) as conn:
                yield conn, metadata.tables

        self.enterContext(patch.object(repo, 'connection', connection))
        self.category = {'id': 42, 'full_path': '宠物 > 粘毛器'}

    def save(self, pages, actual, *, error=False):
        batch = uuid4().hex
        ids = repo.begin(None, batch, uuid4().hex, self.category, '7d', pages, ['search'])
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
