"""目录重采事务的回归用例。"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage import publish_categories as repo
from ecommerce.infrastructure.storage.mysql_migrations.schema_v27 import build_metadata, NEW_TABLES


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

    def scan(self, rows, complete, failed=()):
        identifier = uuid4().hex
        repo.begin_scan(self.settings, identifier, ['宠物'])
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
