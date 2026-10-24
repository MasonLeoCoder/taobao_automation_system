"""独立验证库中的精简迁移：拒绝误删、DDL中断恢复及业务数据保留。"""
from dataclasses import replace
from datetime import datetime
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage import mysql
from ecommerce.infrastructure.storage.mysql_migrations import schema_v4, schema_v5
from ecommerce.infrastructure.tasks.lock import all_browser_resources
from ecommerce.shared.config import load_settings
from tests.mysql_migration_fixture import reset_empty_validation


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_MIGRATION_TESTS')=='1', '显式启用空验证库迁移演练')
class RemoveHistoryImportMigrationTests(unittest.TestCase):
    def setUp(self):
        """锁定资源并准备空旧版验证库；参数：无；返回：无。"""
        self.settings=load_settings()
        self.enterContext(all_browser_resources(self.settings))
        self.config=replace(self.settings.mysql,database='taobao_automation_verify')
        self.engine=mysql.create_mysql_engine(self.config,migration=True)
        self.addCleanup(self.engine.dispose)
        self.task_id=uuid4().hex
        self.import_id=None
        self.product_id=None
        self.batch_id=None
        with self.engine.connect() as connection:
            reset_empty_validation(connection,schema_v4.build_metadata(),schema_v4.REVISION)
        self.addCleanup(self.restore_empty_head)

    def restore_empty_head(self):
        """只清理本例合成身份并恢复最新结构；参数：无；返回：无。"""
        with self.engine.begin() as connection:
            metadata=sa.MetaData()
            metadata.reflect(connection)
            t=metadata.tables
            if self.product_id:
                connection.execute(t['product_current_sections'].delete().where(t['product_current_sections'].c.product_id==self.product_id))
            if self.batch_id:
                connection.execute(t['parameter_observations'].delete().where(t['parameter_observations'].c.batch_id==self.batch_id))
                connection.execute(t['collection_batches'].delete().where(t['collection_batches'].c.id==self.batch_id))
            connection.execute(t['task_events'].delete().where(t['task_events'].c.task_id==self.task_id))
            connection.execute(t['tasks'].delete().where(t['tasks'].c.id==self.task_id))
            if self.product_id:
                connection.execute(t['products'].delete().where(t['products'].c.id==self.product_id))
            if self.import_id and 'import_runs' in t:
                connection.execute(t['import_runs'].delete().where(t['import_runs'].c.id==self.import_id))
        mysql.upgrade_schema(self.config)

    def seed(self, running=False):
        """建立关联业务样例；参数：是否未结束；返回：无。"""
        t=schema_v4.build_metadata().tables
        instant=datetime(2026,10,24,20,0,0,123456)
        with self.engine.begin() as connection:
            connection.execute(t['tasks'].insert().values(id=self.task_id,name='example.remove_import',origin='live',
                status='running' if running else 'succeeded',started_at=instant,finished_at=None if running else instant,last_event_sequence=1))
            if running:
                return
            connection.execute(t['task_events'].insert().values(task_id=self.task_id,task_sequence=1,event_key=uuid4().hex,
                event_type='task_finished',origin='live',occurred_at=instant,recorded_at=instant,time_basis='explicit_offset',message='合成完成'))
            self.product_id=connection.execute(t['products'].insert().values(platform='taobao',platform_product_id=str(int(uuid4().hex[:14],16)))).inserted_primary_key[0]
            self.batch_id=connection.execute(t['collection_batches'].insert().values(platform='taobao',module='parameters',source_batch_key=uuid4().hex,
                task_id=self.task_id,product_id=self.product_id,status='complete',scope_key='a'*64,coverage_status='unknown',visibility='published',
                captured_at=instant,result_payload={'record':{'status':'complete','示例':'中文内容保持原样'}})).inserted_primary_key[0]
            connection.execute(t['parameter_observations'].insert().values(batch_id=self.batch_id,position=1,name='品牌',value='合成样例',status='read'))
            connection.execute(t['product_current_sections'].insert().values(product_id=self.product_id,section='parameters',module='parameters',scope_key='a'*64,
                batch_id=self.batch_id,published_at=instant,publication_basis='module_completeness_checked'))

    def business_rows(self):
        """读取删列之外的所有业务值；参数：无；返回：稳定排序的记录。"""
        with self.engine.connect() as connection:
            actual=sa.MetaData()
            actual.reflect(connection)
            return {name:[dict(row) for row in connection.execute(sa.select(*[c for c in table.c if c.name!='import_run_id']).order_by(*table.primary_key.columns)).mappings()]
                    for name,table in actual.tables.items() if name not in schema_v5.IMPORT_TABLES and name!='alembic_version'}

    def test_running_task_blocks_before_any_ddl(self):
        """活动任务存在时不删除任何导入字段或表。"""
        self.seed(running=True)
        with self.assertRaisesRegex(RuntimeError,'未结束任务'):
            mysql.upgrade_schema(self.config)
        with self.engine.connect() as connection:
            self.assertEqual(mysql.current_revision(connection),schema_v4.REVISION)
            self.assertTrue(sa.inspect(connection).has_table('import_runs'))
            self.assertIn('import_run_id',{c['name'] for c in sa.inspect(connection).get_columns('tasks')})

    def test_import_records_block_before_any_ddl(self):
        """即使只有失败导入记录，也不能自动删除或清空。"""
        table=schema_v4.build_metadata().tables['import_runs']
        with self.engine.begin() as connection:
            self.import_id=connection.execute(table.insert().values(started_at=datetime(2026,10,24),status='failed',manifest_sha256='b'*64,parser_version='synthetic')).inserted_primary_key[0]
        with self.assertRaisesRegex(RuntimeError,'导入表仍有记录'):
            mysql.upgrade_schema(self.config)
        with self.engine.connect() as connection:
            self.assertEqual(mysql.current_revision(connection),schema_v4.REVISION)
            self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(table)),1)
            self.assertIn('import_run_id',{c['name'] for c in sa.inspect(connection).get_columns('tasks')})

    def test_partial_ddl_and_version_failure_resume_preserving_business_rows(self):
        """删列后、推进版本前分别中断；重试和重复执行保留全部业务值。"""
        self.seed()
        before=self.business_rows()
        for phase in ('column','version'):
            injected=[]
            def interrupt(connection,cursor,statement,parameters,context,executemany):
                """在明确阶段注入一次故障；参数：数据库回调；返回：异常。"""
                matched=('DROP COLUMN import_run_id' in statement if phase=='column' else
                    statement.lstrip().upper().startswith('UPDATE ALEMBIC_VERSION') and (schema_v5.REVISION in statement or schema_v5.REVISION in str(parameters)))
                if not injected and matched:
                    injected.append(True)
                    raise RuntimeError('合成精简迁移中断')
            event='after_cursor_execute' if phase=='column' else 'before_cursor_execute'
            sa.event.listen(self.engine,event,interrupt)
            try:
                with patch.object(mysql,'create_mysql_engine',return_value=self.engine),self.assertRaisesRegex(RuntimeError,'精简迁移中断'):
                    mysql.upgrade_schema(self.config)
            finally:
                sa.event.remove(self.engine,event,interrupt)
            self.assertTrue(injected)
            with self.engine.connect() as connection:
                self.assertEqual(mysql.current_revision(connection),schema_v4.REVISION)
            self.assertEqual(self.business_rows(),before)
        self.assertEqual(mysql.upgrade_schema(self.config),schema_v5.REVISION)
        self.assertEqual(mysql.upgrade_schema(self.config),schema_v5.REVISION)
        self.assertEqual(self.business_rows(),before)
        report=mysql.inspect_schema(self.config)
        self.assertEqual(report['schema_errors'],[])
        self.assertEqual((report['table_count'],report['column_count'],report['foreign_key_count']),(19,245,42))
        self.assertTrue(report['all_comments_present'])

    def test_new_database_runs_entire_frozen_migration_chain(self):
        """空库从首版依次升级至精简版，旧版本模型继续可用。"""
        with self.engine.connect() as connection:
            reset_empty_validation(connection,None,None)
        self.assertEqual(mysql.upgrade_schema(self.config),schema_v5.REVISION)
        self.assertEqual(mysql.inspect_schema(self.config)['schema_errors'],[])
