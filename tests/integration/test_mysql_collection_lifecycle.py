"""独立验证库的真实收尾事务检查，全部合成记录在外层事务回滚。"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import os
import unittest
from uuid import uuid4
import sqlalchemy as sa

from ecommerce.infrastructure.storage.mysql import create_mysql_engine,MySQLStorageError
from ecommerce.infrastructure.storage.mysql_migrations.schema_v19 import build_metadata
from ecommerce.infrastructure.storage.task_store import MySQLTaskStore
from ecommerce.infrastructure.storage.time_values import BEIJING
from ecommerce.shared.config import load_settings


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','须显式启用独立MySQL验证')
class MySQLCollectionLifecycleTests(unittest.TestCase):
    def test_finish_transaction_rollback_retry_and_idempotence(self):
        """真实MySQL核对同事务回滚/重放、模块保留与迟到进度；参数：无；返回：断言。"""
        settings=load_settings();mysql=replace(settings.mysql,database='taobao_automation_verify')
        self.assertTrue(mysql.database.endswith('_verify'))
        engine=create_mysql_engine(mysql);tables=build_metadata().tables
        try:
            with engine.connect() as connection:
                transaction=connection.begin()
                try:
                    class ScopedEngine:
                        """所有存储提交仅释放保存点，外层事务最终回滚。"""
                        @contextmanager
                        def begin(self):
                            with connection.begin_nested():yield connection
                    store=object.__new__(MySQLTaskStore)
                    store.engine,store.tables,store.settings=ScopedEngine(),tables,mysql
                    task=uuid4().hex;now=datetime.now(BEIJING).replace(tzinfo=None)
                    connection.execute(tables['tasks'].insert().values(id=task,name='collection.taobao.synthetic',
                        status='running',origin='live',started_at=now,last_event_sequence=0))
                    product=connection.execute(tables['products'].insert().values(platform='taobao',platform_product_id='99'+str(int(uuid4().hex,16))[:20])).inserted_primary_key[0]
                    run=connection.execute(tables['collection_runs'].insert().values(source_run_key=task+'-run',
                        task_id=task,product_id=product,status='running',started_at=now)).inserted_primary_key[0]
                    for module,status in (('video','downloaded'),('questions','running'),('reviews','pending')):
                        connection.execute(tables['collection_steps'].insert().values(run_id=run,module=module,status=status,
                            requested=True,continuation_allowed=status=='downloaded',finished_at=now if status=='downloaded' else None,
                            payload={'record':{'status':status}}))
                    op={'op':'finish','task_id':task,'database':mysql.database,'event_key':uuid4().hex,
                        'time':datetime.now(BEIJING).isoformat(),'event_type':'task_finished','state':'cancelled',
                        'message':'合成停止，结束后全部回滚','close_collection_runs':True}
                    def fail(conn,cursor,statement,parameters,context,many):
                        if statement.startswith('INSERT INTO task_events'):
                            raise sa.exc.OperationalError(statement,parameters,Exception('合成写入失败'))
                    sa.event.listen(engine,'before_cursor_execute',fail)
                    try:
                        with self.assertRaises(MySQLStorageError):store.apply_operation(op)
                    finally:sa.event.remove(engine,'before_cursor_execute',fail)
                    self.assertEqual(connection.scalar(sa.select(tables['collection_runs'].c.status).where(tables['collection_runs'].c.id==run)),'running')
                    self.assertEqual(connection.scalar(sa.select(tables['tasks'].c.status).where(tables['tasks'].c.id==task)),'running')
                    store.apply_operation(op);store.apply_operation(op)
                    self.assertEqual(connection.scalar(sa.select(tables['tasks'].c.status).where(tables['tasks'].c.id==task)),'cancelled')
                    self.assertEqual(connection.scalar(sa.select(tables['collection_runs'].c.status).where(tables['collection_runs'].c.id==run)),'interrupted')
                    steps=connection.execute(sa.select(tables['collection_steps']).where(tables['collection_steps'].c.run_id==run)).mappings().all()
                    self.assertEqual({r['module']:r['status'] for r in steps},{'video':'downloaded','questions':'interrupted','reviews':'not_run'})
                    self.assertEqual(next(r for r in steps if r['module']=='questions')['payload']['record']['status'],'running')
                    self.assertEqual(connection.scalar(sa.select(sa.func.count()).select_from(tables['task_events']).where(tables['task_events'].c.task_id==task)),1)
                    with self.assertRaises(MySQLStorageError):
                        store.apply_operation({**op,'op':'progress','state':'running','event_key':uuid4().hex})
                finally:transaction.rollback()
        finally:engine.dispose()
