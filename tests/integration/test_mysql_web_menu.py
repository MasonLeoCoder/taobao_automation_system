"""真实MySQL菜单校验；只使用验证库，修改全部在事务结束时回滚。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch
import os
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql import create_mysql_engine
from ecommerce.infrastructure.storage.mysql_migrations.schema_v25 import build_metadata
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app


@skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS') == '1', '显式启用独立验证库')
class MySQLWebMenuTests(TestCase):
    def setUp(self):
        """建立验证库连接与不启动任务的本地应用；参数：无；返回：无。"""
        target = replace(load_settings().mysql, database='taobao_automation_verify')
        self.assertTrue(target.database.endswith('_verify'))
        self.engine = create_mysql_engine(target)
        self.addCleanup(self.engine.dispose)
        self.connection = self.engine.connect()
        self.addCleanup(self.connection.close)
        self.transaction = self.connection.begin()
        self.addCleanup(self.transaction.rollback)
        self.tables = build_metadata().tables
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = create_app(load_settings(root=Path(temporary.name)))
        self.app.config['WORKSPACE_MENU_DATABASE'] = True
        self.client = self.app.test_client()
        # 同步test_client共用事务，保证业务路由检查真实MySQL新值且不提交测试配置。
        mocked = patch('ecommerce.infrastructure.storage.web_menus.read', side_effect=self.read)
        mocked.start()
        self.addCleanup(mocked.stop)

    def read(self, settings):
        """在本测试事务读取两张真实配置表；参数：配置；返回：配置行。"""
        return tuple([dict(row) for row in self.connection.execute(sa.select(self.tables[name])).mappings()]
                     for name in ('web_menu_groups', 'web_menu_items'))

    def test_seed_foreign_keys_checks_and_database_updated_time(self):
        groups, items = self.read(None)
        self.assertEqual((len(groups), len(items)), (6, 31))
        table = self.tables['web_menu_items']
        before = self.connection.execute(sa.select(table).where(table.c.menu_key == 'ledger')).mappings().one()
        self.connection.execute(table.update().where(table.c.menu_key == 'ledger').values(name='事务内收支账本', is_visible=False))
        after = self.connection.execute(sa.select(table).where(table.c.menu_key == 'ledger')).mappings().one()
        self.assertGreater(after['updated_at'], before['updated_at'])
        self.assertLess(abs((after['updated_at'] - database_now()).total_seconds()), 10)
        for changes, code in ((dict(group_key='missing-group'), 1452), (dict(sort_order=-1), 3819),
                              (dict(construction_status='not-implemented'), 3819)):
            with self.subTest(changes=changes), self.assertRaises(sa.exc.DBAPIError) as caught, self.connection.begin_nested():
                self.connection.execute(table.update().where(table.c.menu_key == 'ledger').values(**changes))
            self.assertEqual(caught.exception.orig.args[0], code)
        group = self.tables['web_menu_groups']
        with self.assertRaises(sa.exc.IntegrityError), self.connection.begin_nested():
            self.connection.execute(group.delete().where(group.c.group_key == 'operations'))
        with self.assertRaises(sa.exc.IntegrityError), self.connection.begin_nested():
            self.connection.execute(group.update().where(group.c.group_key == 'operations').values(group_key='renamed'))
        for field in ('is_enabled', 'is_visible'):
            with self.subTest(field=field), self.assertRaises(sa.exc.DBAPIError) as caught, self.connection.begin_nested():
                self.connection.exec_driver_sql('UPDATE web_menu_items SET '+field+'=2 WHERE menu_key=\'ledger\'')
            self.assertEqual(caught.exception.orig.args[0], 3819)
        with self.assertRaises(sa.exc.IntegrityError), self.connection.begin_nested():
            self.connection.execute(table.insert().values(menu_key='ledger', name='重复主键', group_key='operations', construction_status='planned'))

    def test_mysql_edit_refresh_hide_disable_parent_and_preserve_existing_config(self):
        items, groups = self.tables['web_menu_items'], self.tables['web_menu_groups']
        self.connection.execute(items.update().where(items.c.menu_key == 'ledger').values(
            name='真实MySQL事务账本', sort_order=0, group_key='research', is_visible=False))
        response = self.client.get('/plans/ledger')
        self.assertEqual(response.status_code, 200)
        self.assertIn('<h1>真实MySQL事务账本</h1>', response.get_data(as_text=True))
        self.connection.execute(groups.update().where(groups.c.group_key == 'research').values(is_enabled=False))
        self.assertEqual(self.client.get('/plans/ledger').status_code, 403)
        self.connection.execute(groups.update().where(groups.c.group_key == 'research').values(is_enabled=True))
        self.assertEqual(self.client.get('/plans/ledger').status_code, 200)
        # 使用冻结种子补缺函数，不能改写刚才的人工设置。
        from importlib import import_module
        migration = import_module('ecommerce.infrastructure.storage.mysql_migrations.versions.0025_web_menu')
        migration.seed_missing(self.connection)
        row = self.connection.execute(sa.select(items).where(items.c.menu_key == 'ledger')).mappings().one()
        self.assertEqual((row['name'], row['sort_order'], row['group_key'], row['is_visible']),
                         ('真实MySQL事务账本', 0, 'research', False))
