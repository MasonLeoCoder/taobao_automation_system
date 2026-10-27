"""仅在空验证库演练详情来源DDL中断重入和重复升级。"""
import os
from dataclasses import replace
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from ecommerce.infrastructure.storage import mysql
from ecommerce.infrastructure.storage.mysql_migrations import schema_v3, schema_v4
from ecommerce.infrastructure.tasks.lock import all_browser_resources
from tests.mysql_migration_fixture import reset_empty_validation
from ecommerce.shared.config import load_settings


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_MIGRATION_TESTS')=='1','显式启用空验证库迁移演练')
class ListDetailMigrationTests(unittest.TestCase):
    def test_partial_column_creation_can_resume_and_upgrade_is_idempotent(self):
        """在首列已提交后注入异常，重入核对原列，再验证最终结构和重复升级。"""
        settings=load_settings()
        config=replace(settings.mysql,database='taobao_automation_verify')
        engine=mysql.create_mysql_engine(config,migration=True)
        self.addCleanup(engine.dispose)
        with all_browser_resources(settings):
            with engine.connect() as connection:
                reset_empty_validation(connection,schema_v3.build_metadata(),schema_v3.REVISION)
                metadata=sa.MetaData()
                metadata.reflect(connection)
                self.assertTrue(all(connection.scalar(sa.select(sa.func.count()).select_from(t))==0
                    for name,t in metadata.tables.items() if name!='alembic_version'),'只允许在空验证库演练')
                revision=mysql.current_revision(connection)
                self.assertIn(revision,(schema_v4.PREVIOUS_REVISION,schema_v4.REVISION))
            injected=[]
            def interrupt(connection,cursor,statement,parameters,context,executemany):
                """在MySQL提交新增列后模拟中断；参数：数据库回调；返回：抛出明确异常。"""
                if not injected and 'ALTER TABLE search_records ADD COLUMN source_list_observation_id' in statement:
                    injected.append(True)
                    raise RuntimeError('合成详情来源列创建后中断')
            sa.event.listen(engine,'after_cursor_execute',interrupt)
            try:
                with patch.object(mysql,'create_mysql_engine',return_value=engine),self.assertRaisesRegex(RuntimeError,'创建后中断'):
                    mysql.upgrade_schema(config)
            finally:
                sa.event.remove(engine,'after_cursor_execute',interrupt)
            try:
                self.assertTrue(injected)
                with engine.connect() as connection:
                    self.assertEqual(mysql.current_revision(connection),schema_v4.PREVIOUS_REVISION)
                    self.assertIn('source_list_observation_id',{c['name'] for c in sa.inspect(connection).get_columns('search_records')})
                self.assertEqual(mysql.upgrade_schema(config),mysql.REVISION)
                self.assertEqual(mysql.upgrade_schema(config),mysql.REVISION)
                report=mysql.inspect_schema(config)
                self.assertEqual(report['schema_errors'],[])
                self.assertEqual((report['table_count'],report['column_count'],report['foreign_key_count']),(len(mysql.build_metadata().tables)+1,sum(len(t.c) for t in mysql.build_metadata().tables.values())+1,sum(len(t.foreign_key_constraints) for t in mysql.build_metadata().tables.values())))
                self.assertTrue(report['all_comments_present'])
            finally:
                mysql.upgrade_schema(config)
