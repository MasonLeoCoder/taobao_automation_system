"""空验证库演练统一申请结构的中断重入及旧队列保留，不连接真实平台。"""
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4
import os
import unittest
import sqlalchemy as sa
from ecommerce.infrastructure.storage import mysql
from ecommerce.infrastructure.storage.mysql_migrations import schema_v8
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.tasks.lock import all_browser_resources
from ecommerce.shared.config import load_settings
from tests.mysql_migration_fixture import reset_empty_validation


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_MIGRATION_TESTS')=='1','须显式启用空验证库迁移演练')
class CollectionJobsMigrationTests(unittest.TestCase):
    def test_partial_create_and_old_pending_queue_migrate_without_start(self):
        settings=load_settings();config=replace(settings.mysql,database='taobao_automation_verify')
        engine=mysql.create_mysql_engine(config,migration=True);self.addCleanup(engine.dispose)
        job=uuid4().hex;key=uuid4().hex*2
        with all_browser_resources(settings):
            try:
                with engine.connect() as c:
                    reset_empty_validation(c,schema_v8.build_metadata(),schema_v8.REVISION)
                    old=schema_v8.build_metadata().tables['collection_retry_queue']
                    c.execute(old.insert().values(id=job,execution_key=key,request_key='f'*64,state='waiting',
                        payload={'product_id':'123','module':'video','options':{}},created_at=database_now(),updated_at=database_now()))
                    c.commit()
                original=sa.Table.create
                def fail_queue(table,*args,**kwargs):
                    if table.name=='collection_queue_states':
                        raise RuntimeError('合成第二张表建表中断')
                    return original(table,*args,**kwargs)
                with patch.object(sa.Table,'create',fail_queue),self.assertRaises(RuntimeError):
                    mysql.upgrade_schema(config)
                self.assertEqual(mysql.upgrade_schema(config),mysql.REVISION)
                self.assertEqual(mysql.upgrade_schema(config),mysql.REVISION)
                report=mysql.inspect_schema(config);self.assertEqual(report['schema_errors'],[])
                with engine.connect() as c:
                    self.assertFalse(sa.inspect(c).has_table('collection_retry_queue'))
                    row=c.execute(sa.text('SELECT dispatch_state,task_id FROM collection_jobs WHERE id=:id'),{'id':job}).one()
                    self.assertEqual(tuple(row),('paused',None))
                    self.assertEqual(c.scalar(sa.text('SELECT COUNT(*) FROM tasks')),0)
                    self.assertEqual(c.scalar(sa.text('SELECT paused FROM collection_queue_states WHERE execution_key=:key'),{'key':key}),1)
            finally:
                with engine.begin() as c:
                    if sa.inspect(c).has_table('collection_jobs'):
                        c.execute(sa.text('DELETE FROM collection_jobs WHERE id=:id'),{'id':job})
                    if sa.inspect(c).has_table('collection_queue_states'):
                        c.execute(sa.text('DELETE FROM collection_queue_states WHERE execution_key=:key'),{'key':key})
                    if sa.inspect(c).has_table('collection_retry_queue'):
                        c.execute(sa.text('DELETE FROM collection_retry_queue WHERE id=:id'),{'id':job})
                mysql.upgrade_schema(config)
