"""收尾事务、乱序和历史只读核对的隔离测试，不访问正式库。"""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_lifecycle import run_views,task_views
from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from ecommerce.infrastructure.tasks.events import TaskEvents
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention
from tests.job_store_fixture import JobDatabaseFixture


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        """创建带约束的临时数据库；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=replace(load_settings(root=Path(temp.name)),task_backend='mysql',collection_backend='mysql')
        self.db=JobDatabaseFixture(self,self.settings);self.tables=self.db.tables;self.engine=self.db.engine
        self.task='a'*32;self.other='b'*32;self.now=datetime(2026,10,31,20,0)
        for identifier in (self.task,self.other):
            self.put('tasks',id=identifier,name='collection.taobao.synthetic',status='running',origin='live',started_at=self.now)
        self.put('products',id=1,platform='taobao',platform_product_id='123')
        for identifier,task,status in ((1,self.task,'running'),(2,self.task,'complete'),(3,self.other,'running')):
            self.put('collection_runs',id=identifier,source_run_key='run-'+str(identifier),task_id=task,product_id=1,status=status,
                     started_at=self.now,finished_at=self.now if status=='complete' else None)
        for identifier,module,status,requested in ((1,'video','downloaded',True),(2,'questions','running',True),
                (3,'reviews','pending',True),(4,'parameters','not_requested',False)):
            self.put('collection_steps',id=identifier,run_id=1,module=module,status=status,requested=requested,
                     continuation_allowed=status=='downloaded',started_at=self.now if status!='pending' else None,
                     finished_at=self.now if status=='downloaded' else None,payload={'record':{'status':status,'original':True}})

    def put(self,table_name,**values):
        """插入合成样例；参数：表、字段；返回：无。"""
        with self.engine.begin() as connection:connection.execute(self.tables[table_name].insert().values(**values))

    def read(self,name):
        """读取完整合成记录；参数：表；返回：行列表。"""
        with self.engine.connect() as connection:return [dict(r) for r in connection.execute(sa.select(self.tables[name]).order_by(self.tables[name].c.id)).mappings()]

    def operation(self,**values):
        """构造新版本结束事件；参数：覆盖值；返回：操作。"""
        return {'op':'finish','task_id':self.task,'database':self.settings.mysql.database,'event_key':uuid4().hex,
                'time':'2026-10-31T20:02:04+08:00','event_type':'task_finished','state':'cancelled',
                'message':'用户已停止任务','close_collection_runs':True,**values}

    def test_atomic_finish_idempotent_and_completed_other_task_untouched(self):
        """仅收尾同任务未结束部分，重复事件幂等，快照不改；参数：无；返回：断言。"""
        before=self.read('collection_steps');op=self.operation()
        with self.engine.begin() as c:
            c.execute(self.tables['collection_runs'].update().where(self.tables['collection_runs'].c.id==1).values(error={}))
        self.db.store.apply_operation(op);self.db.store.apply_operation(op)
        runs=self.read('collection_runs');steps=self.read('collection_steps')
        self.assertEqual([r['status'] for r in runs],['interrupted','complete','running'])
        self.assertEqual(runs[0]['error'],{})
        self.assertEqual([r['status'] for r in steps],['downloaded','interrupted','not_run','not_requested'])
        self.assertEqual(steps[0],before[0]);self.assertEqual(steps[3],before[3])
        self.assertEqual([r['payload'] for r in steps],[r['payload'] for r in before])
        self.assertEqual(len(self.read('task_events')),1)
        self.assertEqual(self.read('tasks')[0]['status'],'cancelled')
        with self.assertRaises(MySQLStorageError):
            self.db.store.apply_operation(self.operation(op='progress',state='running'))
        self.assertEqual(self.read('tasks')[0]['status'],'cancelled')
        with self.engine.connect() as c:
            view=run_views(c,self.tables,[runs[0]])[0]['display']
        self.assertEqual(view['state'],'cancelled');self.assertEqual(view['counts'],{'requested':3,'completed':1,'unfinished':2})

    def test_failure_rolls_back_task_runs_steps_and_event_then_replay_succeeds(self):
        """收尾中途写入失败全部回滚，同事件可安全补写；参数：无；返回：断言。"""
        before={name:self.read(name) for name in ('tasks','collection_runs','collection_steps','task_events')}
        def fail(connection,cursor,statement,parameters,context,many):
            if statement.startswith('INSERT INTO task_events'):raise sa.exc.OperationalError(statement,parameters,Exception('模拟断库'))
        op=self.operation();sa.event.listen(self.engine,'before_cursor_execute',fail)
        try:
            with self.assertRaises(MySQLStorageError):self.db.store.apply_operation(op)
        finally:sa.event.remove(self.engine,'before_cursor_execute',fail)
        for name,rows in before.items():self.assertEqual(self.read(name),rows)
        self.db.store.apply_operation(op)
        self.assertEqual(self.read('collection_runs')[0]['status'],'interrupted')

    def test_old_finish_no_backfill_and_read_view_corrects_old_running(self):
        """旧事件不自动回填历史，读取关联终态给出校正提示；参数：无；返回：断言。"""
        op=self.operation();op.pop('close_collection_runs');self.db.store.apply_operation(op)
        before=self.read('collection_runs');self.assertEqual(before[0]['status'],'running')
        with self.engine.connect() as c:
            view=run_views(c,self.tables,before)[0]['display']
        self.assertEqual(view['state'],'cancelled');self.assertIn('未同步',view['note'])
        self.assertEqual(str(view['finished_at']),'2026-10-31 20:02:04')
        self.assertEqual(self.read('collection_runs'),before)

    def test_alive_unknown_exited_stopping_and_success_conflict(self):
        """不凭等待时间猜状态，不把总任务成功当详情成功；参数：无；返回：断言。"""
        for live,expected in ((True,'running'),(False,'interrupted'),(None,'unverified')):
            with patch('ecommerce.infrastructure.storage.collection_lifecycle.owner_alive',return_value=live),self.engine.connect() as c:
                self.assertEqual(run_views(c,self.tables,self.read('collection_runs'))[0]['display']['state'],expected)
        self.put('collection_queue_states',execution_key='c'*64,paused=False,revision=1,updated_at=self.now)
        self.put('collection_jobs',id=self.task,execution_key='c'*64,origin='web',dispatch_state='started',task_id=self.task,
                 stop_requested_at=self.now,revision=1,payload={},created_at=self.now,updated_at=self.now)
        with patch('ecommerce.infrastructure.storage.collection_lifecycle.owner_alive',return_value=True),self.engine.connect() as c:
            self.assertEqual(run_views(c,self.tables,self.read('collection_runs'))[0]['display']['state'],'stopping')
            self.assertEqual(task_views(c,self.tables,self.read('tasks'))[0]['display']['state'],'stopping')
        op=self.operation(state='succeeded');op.pop('close_collection_runs');self.db.store.apply_operation(op)
        with self.engine.connect() as c:self.assertEqual(run_views(c,self.tables,self.read('collection_runs'))[0]['display']['state'],'unverified')

    def test_session_stop_repeated_and_late_progress(self):
        """重复结束不追加冲突事件，终态后进度不排入流水；参数：无；返回：断言。"""
        from tests.unit.test_task_events import MemoryStore
        session=TaskEvents(self.settings,MemoryStore(self.settings.mysql),'collection.taobao.synthetic')
        with patch.object(session.journal,'pending',return_value=False):
            self.assertTrue(session.finish('cancelled','停止'));self.assertTrue(session.finish('cancelled','停止'))
            session.progress('running','迟到进度')
            with self.assertRaises(NeedsAttention):session.finish('succeeded','假完成')
        import json
        operations=[json.loads(line) for line in session.journal.path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(operations),1);self.assertTrue(operations[0]['close_collection_runs'])

    def test_finish_local_append_failure_keeps_same_identity_for_retry(self):
        """结束事件首次落盘失败不能被重复结束误认作成功；参数：无；返回：断言。"""
        from tests.unit.test_task_events import MemoryStore
        session=TaskEvents(self.settings,MemoryStore(self.settings.mysql),'collection.taobao.synthetic')
        with patch.object(session,'append',side_effect=NeedsAttention('合成磁盘失败')):
            with self.assertRaises(NeedsAttention):session.finish('cancelled','停止')
        key=session.finish_operation['event_key']
        self.assertFalse(getattr(session,'finish_queued',False))
        with patch.object(session.journal,'pending',return_value=False):self.assertTrue(session.finish('cancelled','停止'))
        self.assertEqual(session.finish_operation['event_key'],key)
        self.assertTrue(session.finish_queued)

    def test_stop_requested_with_unknown_process_remains_unverified(self):
        """停止申请不能覆盖进程身份不明的提示；参数：无；返回：断言。"""
        from ecommerce.modules.collection.web_collection import public_status
        row={'id':self.task,'product_id':'123','module':'questions','state':'running','dispatch_state':'started',
             'owner_pid':42,'stop_requested':True,'created_at':'2026-10-31T20:00:00+08:00'}
        with patch('ecommerce.modules.collection.web_collection.jobs.alive',return_value=None):
            self.assertEqual(public_status(self.settings,row)['state'],'unverified')

    def test_late_report_does_not_reopen_finished_run_or_completed_step(self):
        """迟到运行快照不重开终态，运行中也不回退已完成模块；参数：无；返回：断言。"""
        from ecommerce.infrastructure.storage.collection_store import CollectionWriter
        header={'kind':'report','task_id':self.task,'product_id':'123','logical_key':'late-test',
                'values':{'source_run_key':'run-1','status':'running','started_at':'2026-10-31T20:00:00+08:00'}}
        child={'table':'collection_steps','key':{'module':'video'},'values':{'module':'video','requested':True,
               'status':'running','continuation_allowed':False,'started_at':'2026-10-31T20:00:00+08:00','finished_at':None}}
        with patch('ecommerce.infrastructure.storage.collection_store.open_snapshot',side_effect=lambda *a:(header,(r for r in [child]))),self.engine.begin() as c:
            before=self.read('collection_steps')[0]
            CollectionWriter(self.settings,c).apply({'record_count':1,'sha256':'c'*64})
            self.assertEqual(self.read('collection_steps')[0],before)
        with self.engine.begin() as c:
            c.execute(self.tables['collection_runs'].update().where(self.tables['collection_runs'].c.id==1).values(status='interrupted',finished_at=self.now))
        before=self.read('collection_runs')
        with patch('ecommerce.infrastructure.storage.collection_store.open_snapshot',side_effect=lambda *a:(header,(r for r in []))),self.engine.begin() as c:
            result=CollectionWriter(self.settings,c).apply({'record_count':0,'sha256':'d'*64})
        self.assertEqual(result['issues'][0]['code'],'late_run_progress')
        self.assertEqual(self.read('collection_runs'),before)
