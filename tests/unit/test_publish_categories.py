"""目录重采事务的回归用例。"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

import sqlalchemy as sa
from flask import Flask

from ecommerce.infrastructure.storage import publish_categories as repo
from ecommerce.infrastructure.storage.mysql_migrations.schema_v27 import build_metadata, NEW_TABLES
from ecommerce.platforms.qianniu import publish_categories as platform
from ecommerce.web.routes.publish_categories import publish_categories


class PublishCategoryRouteTests(TestCase):
    def test_new_scans_allow_only_one_branch_and_legacy_failed_scopes_can_retry(self):
        app = Flask(__name__)
        app.config.update(SETTINGS=SimpleNamespace(collection_backend='mysql'), COLLECTION_TOKEN='token')
        app.register_blueprint(publish_categories)
        start = Mock(return_value={'phase': 'queued'})
        app.extensions['publish_categories'] = SimpleNamespace(start=start)
        client = app.test_client()
        headers = {'X-Collection-Token': 'token'}

        def submit(scopes):
            return client.post('/api/publish-categories/scan', json={'scopes': scopes}, headers=headers)

        rows = [{'full_path': '宠物', 'is_leaf': False}]
        history = [{'scopes': ['宠物', '图书'], 'result': {'failed': [{'path': '', 'reason': '中断'}]}}]
        with patch.object(repo, 'catalog', return_value=(rows, history)):
            self.assertEqual(submit(['宠物', '图书']).status_code, 409)
            start.assert_not_called()
            self.assertEqual(submit([]).status_code, 202)
            self.assertEqual(submit(['宠物']).status_code, 202)
            self.assertEqual(submit(['图书']).status_code, 202)
            self.assertEqual(submit(['未确认分支']).status_code, 409)
        self.assertEqual([call.args[0] for call in start.call_args_list], [[], ['宠物'], ['图书']])


class PublishCategoryScopeTests(TestCase):
    def setUp(self):
        self.page = Mock()
        self.page.locator.return_value.first.is_visible.return_value = True
        self.columns = {
            0: {'宠物': {'platformId': '1', 'branch': True}, '图书': {'platformId': '2', 'branch': True},
                '充值': {'platformId': '3', 'branch': False}},
            1: {'工具': {'platformId': '4', 'branch': True}},
            2: {'粘毛器': {'platformId': '5', 'branch': False}},
        }

    def test_empty_scope_reads_first_column_without_opening_any_category(self):
        with patch.object(platform, '_read_all', side_effect=lambda page, column, **kwargs: self.columns[column]) as read, \
                patch.object(platform, '_pick') as pick:
            observed, complete, failed = platform.collect_publish_categories(self.page, [])
        read.assert_called_once_with(self.page, 0, expand_groups=True)
        pick.assert_not_called()
        self.assertEqual(observed, {('宠物',): ('1', False), ('图书',): ('2', False), ('充值',): ('3', True)})
        self.assertEqual((complete, failed), ({()}, []))

    def test_target_only_opens_that_branch_and_its_children(self):
        with patch.object(platform, '_read_all', side_effect=lambda page, column, **kwargs: self.columns[column]), \
                patch.object(platform, '_pick') as pick:
            observed, complete, failed = platform.collect_publish_categories(self.page, ['宠物'])
        self.assertEqual([call.args[2] for call in pick.call_args_list], ['宠物', '工具'])
        self.assertEqual(set(observed), {('宠物',), ('宠物', '工具'), ('宠物', '工具', '粘毛器')})
        self.assertEqual((complete, failed), ({('宠物',), ('宠物', '工具')}, []))

    def test_interrupted_first_column_is_never_confirmed(self):
        with patch.object(platform, '_read_all', side_effect=platform.CategoryPageError('未读到底')), \
                patch.object(platform, '_pick') as pick:
            observed, complete, failed = platform.collect_publish_categories(self.page, [])
        self.assertEqual((observed, complete, failed), ({}, set(), [{'path': '', 'reason': '未读到底'}]))
        pick.assert_not_called()


class PublishCategoryMergeTests(TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        original = build_metadata()
        metadata = sa.MetaData()
        tables = []
        for name in NEW_TABLES:
            columns = [sa.Column(col.name, sa.Integer if col.name == 'id' and name == NEW_TABLES[0] else col.type,
                                 primary_key=col.primary_key, nullable=col.nullable)
                       for col in original.tables[name].columns]
            tables.append(sa.Table(name, metadata, *columns))
        metadata.create_all(self.engine)
        self.category, self.batch = tables
        self.settings = SimpleNamespace()

        @contextmanager
        def connection(settings, *, write=False):
            with (self.engine.begin() if write else self.engine.connect()) as conn:
                yield conn, self.category, self.batch

        self.enterContext(patch.object(repo, 'connection', connection))

    def scan(self, rows, complete, failed=(), *, scopes=('宠物',)):
        identifier = uuid4().hex
        repo.begin_scan(self.settings, identifier, list(scopes))
        status = repo.apply_scan(self.settings, identifier, rows, complete, list(failed),
                                 {'宠物 > 工具 > 粘毛器': 'lint_roller'})
        return status, next(row['result'] for row in repo.catalog(self.settings)[1] if row['id'] == identifier)

    def initial(self):
        return {('宠物',): ('1', False), ('宠物', '工具'): ('2', False),
                ('宠物', '工具', '粘毛器'): ('3', True), ('宠物', '猫咪'): (None, True)}

    def test_repeat_and_failed_column_preserve_records(self):
        rows = self.initial()
        self.assertEqual(len(self.scan(rows, {('宠物',), ('宠物', '工具')})[1]['added']), 4)
        self.assertEqual(self.scan(rows, {('宠物',), ('宠物', '工具')})[1]['added'], [])
        lint = repo.find(self.settings, '宠物 > 工具 > 粘毛器')
        del rows[('宠物', '工具', '粘毛器')]
        status, result = self.scan(rows, {('宠物',)}, [{'path': '宠物 > 工具', 'reason': '超时'}])
        self.assertEqual((status, result['hidden']), ('partial', []))
        self.assertEqual(repo.find(self.settings, lint['full_path'])['id'], lint['id'])
        self.assertTrue(repo.find(self.settings, lint['full_path'])['is_visible'])

    def test_known_id_rename_preserves_binding_and_requires_review(self):
        rows = self.initial()
        self.scan(rows, {('宠物',), ('宠物', '工具')})
        before = repo.find(self.settings, '宠物 > 工具 > 粘毛器')
        rows[('宠物', '工具', '宠物粘毛器')] = rows.pop(('宠物', '工具', '粘毛器'))
        self.assertEqual(self.scan(rows, {('宠物',), ('宠物', '工具')})[1]['added'], [])
        after = repo.find(self.settings, '宠物 > 工具 > 宠物粘毛器')
        self.assertEqual((after['id'], after['listing_profile_key'], after['profile_status']),
                         (before['id'], 'lint_roller', 'review'))

    def test_conflicting_id_does_not_replace_binding(self):
        rows = self.initial()
        self.scan(rows, {('宠物',), ('宠物', '工具')})
        rows[('宠物', '工具', '粘毛器')] = ('9', True)
        status, result = self.scan(rows, {('宠物',), ('宠物', '工具')})
        lint = repo.find(self.settings, '宠物 > 工具 > 粘毛器')
        self.assertEqual((status, len(result['conflicts']), result['hidden']), ('partial', 1, []))
        self.assertEqual((lint['qianniu_category_id'], lint['profile_status']), ('3', 'review'))

    def test_parent_that_becomes_leaf_hides_previous_children(self):
        rows = self.initial()
        self.scan(rows, {('宠物',), ('宠物', '工具')})
        del rows[('宠物', '工具', '粘毛器')]
        rows[('宠物', '工具')] = ('2', True)
        status, result = self.scan(rows, {('宠物',)})
        self.assertEqual((status, result['hidden']), ('complete', ['宠物 > 工具 > 粘毛器']))
        self.assertFalse(repo.find(self.settings, '宠物 > 工具 > 粘毛器')['is_visible'])

    def test_roots_only_keeps_children_until_a_root_disappears_and_branch_is_confirmed_again(self):
        rows = self.initial()
        root_rows = {('宠物',): ('1', False), ('图书',): ('5', False)}
        self.scan(rows, {('宠物',), ('宠物', '工具')})
        path = '宠物 > 工具 > 粘毛器'
        child_before = repo.find(self.settings, path)
        status, result = self.scan(root_rows, {()}, scopes=())
        self.assertEqual((status, result['mode'], result['completed']), ('complete', 'roots', ['']))
        self.assertEqual(repo.find(self.settings, path), child_before)
        status, result = self.scan({}, set(), [{'path': '', 'reason': '未读到底'}], scopes=())
        self.assertEqual((status, result['hidden']), ('failed', []))
        self.assertEqual(repo.find(self.settings, path), child_before)
        self.scan({('图书',): ('5', False)}, {()}, scopes=())
        self.assertFalse(repo.find(self.settings, path)['is_visible'])
        self.scan(root_rows, {()}, scopes=())
        self.assertTrue(repo.find(self.settings, '宠物')['is_visible'])
        self.assertFalse(repo.find(self.settings, path)['is_visible'])
        self.scan(rows, {('宠物',), ('宠物', '工具')})
        restored = repo.find(self.settings, path)
        self.assertEqual((restored['is_visible'], restored['id'], restored['profile_status']),
                         (True, child_before['id'], 'review'))

    def test_renamed_root_marks_old_children_unavailable_until_rechecked(self):
        self.scan(self.initial(), {('宠物',), ('宠物', '工具')})
        old_id = repo.find(self.settings, '宠物')['id']
        self.scan({('新宠物',): ('1', False)}, {()}, scopes=())
        self.assertEqual(repo.find(self.settings, '新宠物')['id'], old_id)
        self.assertFalse(repo.find(self.settings, '宠物 > 工具 > 粘毛器')['is_visible'])

    def test_interruption_and_failure_retain_the_new_scan_mode(self):
        old, new = uuid4().hex, uuid4().hex
        repo.begin_scan(self.settings, old, [])
        repo.begin_scan(self.settings, new, ['宠物'])
        repo.fail_scan(self.settings, new, '登录失败')
        scans = {row['id']: row for row in repo.catalog(self.settings)[1]}
        self.assertEqual([(scans[key]['status'], scans[key]['result']['mode']) for key in (old, new)],
                         [('failed', 'roots'), ('failed', 'branches')])
