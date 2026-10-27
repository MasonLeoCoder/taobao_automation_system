"""独立MySQL验证统一申请、事务、互斥、迁移及断库停止；不访问真实平台。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict,replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import json
import os
import subprocess
import sys
import tempfile
import unittest
import sqlalchemy as sa
from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore,JobConflict
from ecommerce.infrastructure.storage.collection_job_migration import migrate_dispatch_files
from ecommerce.infrastructure.storage.database import now
from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from ecommerce.infrastructure.tasks import web_jobs
from ecommerce.infrastructure.tasks.ownership import execution_key,process_started_at,atomic_json
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.infrastructure.tasks.collection_control import emergency_stop,replay_emergency
from ecommerce.modules.collection import dispatch,web_collection
from ecommerce.modules.collection.web_worker import execute


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','须显式启用独立验证库')
class MySQLCollectionJobsTests(unittest.TestCase):
    def setUp(self):
        """只使用独立验证库和临时资料；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=replace(load_settings(root=Path(temp.name)),task_backend='mysql',collection_backend='mysql')
        self.assertTrue(self.settings.mysql.database.endswith('_verify'))
        self.platform=self.settings.for_platform('taobao')
        self.key=execution_key(self.platform)
        self.store=CollectionJobStore(self.settings);self.store.tasks.initialize()
        self.t=self.store.tables;self.ids=[]
        self.addCleanup(self.clean)

    def clean(self):
        """只删除本例明确生成的记录；参数：无；返回：无。"""
        j,k,t,e=(self.t[n] for n in ('collection_jobs','collection_job_controls','tasks','task_events'))
        with self.store.engine.begin() as c:
            ids=list(c.scalars(sa.select(j.c.id).where(j.c.execution_key==self.key)))
            c.execute(k.delete().where(k.c.job_id.in_(ids)))
            c.execute(j.delete().where(j.c.id.in_(ids)))
            c.execute(e.delete().where(e.c.task_id.in_(self.ids)))
            c.execute(t.delete().where(t.c.id.in_(self.ids)))
            c.execute(self.t['collection_queue_states'].delete().where(self.t['collection_queue_states'].c.execution_key==self.key))

    def row(self, **changes):
        """生成明确标记的合成申请；参数：覆盖字段；返回：申请。"""
        row={'id':uuid4().hex,'execution_key':self.key,'created_at':now(),'product_id':'123','module':'selected',
             'mode':'detail','modules':['video'],'skipped_modules':[],
             'options':{'mode':'detail','input':'123','modules':['video'],'all_reviews':False,'all_questions':False},
             'config':json.loads(json.dumps(asdict(self.settings),default=str))}
        row.update(changes);self.ids.append(row['id']);return row

    def submit(self,**changes):
        """提交合成申请；参数：覆盖字段；返回：编号。"""
        return self.store.submit(self.row(**changes))

    def claim(self,job_id):
        """领取并绑定本测试进程；参数：编号；返回：申请。"""
        row=self.store.claim(self.key,job_id)
        self.assertIsNotNone(row)
        self.assertTrue(self.store.bind_process(job_id,row['claim_token'],os.getpid(),process_started_at(os.getpid())))
        return self.store.read(job_id)

    def run(self, result=None):
        """保持unittest运行协议；参数：测试结果；返回：父类结果。"""
        return super().run(result)

    def test_submission_response_loss_and_later_intentional_recollect(self):
        row=self.row();submission=uuid4().hex
        job=self.store.submit(row,submission_key=submission)
        claimed=self.claim(job)
        result=run_task(self.platform,'collection.taobao.synthetic',lambda:'仅合成执行，无浏览器',task_id=job,job_claim_token=claimed['claim_token'])
        self.assertEqual(result.status,'succeeded')
        self.assertEqual(self.store.submit(self.row(),submission_key=submission),job)
        self.assertNotEqual(self.store.submit(self.row(),submission_key=uuid4().hex),job)
        with self.assertRaises(JobConflict):
            self.store.submit(self.row(modules=['skus']),submission_key=submission)

    def test_concurrent_submission_and_claim_have_one_winner(self):
        rows=[self.row(),self.row()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            submitted=list(pool.map(self.store.submit,rows))
        self.assertEqual(submitted[0],submitted[1])
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed=list(pool.map(lambda _:self.store.claim(self.key),range(2)))
        self.assertEqual(sum(row is not None for row in claimed),1)

    def test_empty_queue_pause_is_inherited_by_new_requests(self):
        self.store.pause(self.key,True,'合成手动暂停')
        job=self.submit()
        self.assertEqual(self.store.read(job)['dispatch_state'],'paused')
        self.assertIsNone(self.store.claim(self.key))
        self.store.pause(self.key,False,'合成明确继续')
        self.assertEqual(self.store.claim(self.key)['id'],job)

    def test_old_claim_cannot_start_worker_or_overwrite_runtime(self):
        job=self.submit();row=self.claim(job)
        with patch('ecommerce.modules.collection.web_worker.run_collection') as collect:
            execute(self.settings.workspace,job,'0'*32,bootstrap=self.settings)
        collect.assert_not_called()
        with self.assertRaises(JobConflict):
            self.store.runtime({**row,'claim_token':'0'*32,'progress':{'current_module':'skus'}})

    def test_queued_cancel_creates_no_fake_execution(self):
        job=self.submit();self.store.control(job,'stop')
        row=self.store.read(job)
        self.assertEqual(row['dispatch_state'],'cancelled');self.assertIsNone(row['task_id'])
        self.assertIsNone(self.store.claim(self.key))
        with self.store.connection() as c:
            self.assertIsNone(c.scalar(sa.select(self.t['tasks'].c.id).where(self.t['tasks'].c.id==job)))

    def test_stopped_claim_cannot_create_execution(self):
        job=self.submit();row=self.claim(job);self.store.control(job,'stop')
        action=unittest.mock.Mock(return_value='不得执行')
        result=run_task(self.platform,'collection.taobao.synthetic',action,task_id=job,job_claim_token=row['claim_token'])
        action.assert_not_called();self.assertEqual(result.status,'cancelled')
        self.assertIsNone(self.store.read(job)['task_id'])

    def test_execution_creation_and_attachment_roll_back_together(self):
        job=self.submit();row=self.claim(job)
        from ecommerce.infrastructure.storage.collection_jobs import complete_attachment
        def broken(connection,tables,operation):
            complete_attachment(connection,tables,operation)
            raise MySQLStorageError('合成事务回滚')
        with patch('ecommerce.infrastructure.storage.collection_jobs.complete_attachment',side_effect=broken):
            result=run_task(self.platform,'collection.taobao.synthetic',lambda:'不能开始',task_id=job,job_claim_token=row['claim_token'])
        self.assertEqual(result.status,'needs_attention')
        self.assertIsNone(self.store.read(job)['task_id'])
        with self.store.connection() as c:
            self.assertIsNone(c.scalar(sa.select(self.t['tasks'].c.id).where(self.t['tasks'].c.id==job)))

    def test_task_conclusion_and_pause_are_atomic(self):
        job=self.submit();row=self.claim(job)
        waiting=self.submit(module='skus',modules=['skus'])
        from ecommerce.shared.exceptions import TaskStopped
        def stop():
            raise TaskStopped('合成手动停止')
        result=run_task(self.platform,'collection.taobao.synthetic',stop,task_id=job,job_claim_token=row['claim_token'])
        self.assertEqual(result.status,'cancelled')
        self.assertTrue(self.store.queue_state(self.key)['paused'])
        self.assertEqual(self.store.read(waiting)['dispatch_state'],'paused')

    def test_stale_feedback_rejected_and_stop_has_priority(self):
        job=self.submit();row=self.claim(job)
        def action():
            self.store.prompt(job,row['claim_token'],{'prompt_id':'a'*32,'allowed':['继续']})
            with self.assertRaises(JobConflict):self.store.control(job,'continue','b'*32)
            self.store.control(job,'continue','a'*32)
            with self.assertRaises(JobConflict):self.store.control(job,'continue','a'*32)
            self.store.control(job,'stop')
            self.assertEqual(self.store.receive_control(job,row['claim_token'])['action'],'stop')
            return '合成控制验证'
        result=run_task(self.platform,'collection.taobao.synthetic',action,task_id=job,job_claim_token=row['claim_token'])
        self.assertEqual(result.status,'succeeded')

    def test_emergency_stop_replays_before_queue_can_resume(self):
        job=self.submit();row=self.claim(job)
        def action():
            message=emergency_stop(self.platform,job)
            self.assertIn('紧急停止',message)
            self.assertTrue((self.settings.workspace/'storage/collection_emergency_stops'/f'{job}.json').exists())
            replay_emergency(self.settings)
            self.assertTrue(self.store.queue_state(self.key)['paused'])
            self.assertEqual(self.store.receive_control(job,row['claim_token'])['action'],'stop')
            from ecommerce.shared.exceptions import TaskStopped
            raise TaskStopped('合成断库期间停止')
        result=run_task(self.platform,'collection.taobao.synthetic',action,task_id=job,job_claim_token=row['claim_token'])
        self.assertEqual(result.status,'cancelled')
        self.assertFalse((self.settings.workspace/'storage/collection_emergency_stops'/f'{job}.json').exists())

    def test_restart_pauses_waiting_but_keeps_live_claim(self):
        job=self.submit();self.claim(job)
        waiting=self.submit(module='skus',modules=['skus'])
        with patch.object(web_jobs,'launch') as launch:
            dispatch.on_web_start(self.settings)
        launch.assert_not_called()
        self.assertEqual(self.store.read(job)['dispatch_state'],'claimed')
        self.assertEqual(self.store.read(waiting)['dispatch_state'],'paused')

    def test_launch_failure_remains_visible_without_execution(self):
        job=self.submit()
        with patch.object(web_jobs,'launch',side_effect=OSError('合成启动失败')):
            dispatch.dispatch_next(self.settings)
        row=self.store.read(job)
        self.assertEqual(row['dispatch_state'],'start_failed');self.assertIsNone(row['task_id'])
        self.assertTrue(self.store.queue_state(self.key)['paused'])

    def test_local_dispatch_is_migrated_once_and_never_auto_started(self):
        row=self.row();row.update(database=self.settings.mysql.database,state='cancelled',message='合成旧申请')
        atomic_json(self.settings.workspace/'storage/web_collection_jobs'/f"{row['id']}.json",row)
        with patch.object(web_jobs,'launch') as launch:
            first=migrate_dispatch_files(self.settings);second=migrate_dispatch_files(self.settings)
        launch.assert_not_called()
        self.assertEqual(first['created'],[row['id']]);self.assertEqual(second['already_present'],[row['id']])
        self.assertIsNone(self.store.read(row['id'])['task_id']);self.assertFalse(self.store.read(row['id'])['recovery_ready'])

    def test_database_result_survives_missing_or_corrupt_local_dispatch(self):
        job=self.submit()
        directory=self.settings.workspace/'storage/web_collection_jobs';directory.mkdir(parents=True,exist_ok=True)
        (directory/f'{job}.json').write_text('{broken',encoding='utf-8')
        self.assertEqual(web_jobs.read(self.settings,job)['id'],job)
        self.assertEqual(web_collection.history(self.settings)['jobs'][0]['id'],job)

    def test_background_process_reads_database_configuration(self):
        job=self.submit();row=self.store.claim(self.key,job)
        code='''
import sys
from unittest.mock import patch
from ecommerce.modules.collection.web_worker import execute
from ecommerce.infrastructure.tasks.runner import run_task
def synthetic(settings,row):
    return run_task(settings.for_platform('taobao'),'collection.taobao.synthetic',lambda:'isolated synthetic only',task_id=row['id'],job_claim_token=row['claim_token'])
with patch('ecommerce.modules.collection.web_worker.run_collection',side_effect=synthetic):
    execute(sys.argv[1],sys.argv[2],sys.argv[3])
'''
        environment=os.environ.copy()
        environment['ECOMMERCE_JOB_BOOTSTRAP']=json.dumps({'root':str(self.settings.root),'workspace':str(self.settings.workspace),'mysql':asdict(self.settings.mysql)})
        completed=subprocess.run([sys.executable,'-c',code,str(self.settings.workspace),job,row['claim_token']],env=environment,
            cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True,timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.assertEqual(completed.returncode,0,completed.stderr)
        saved=self.store.read(job);self.assertEqual(saved['task_status'],'succeeded');self.assertTrue(saved['actual_finished'])

    def test_cancel_racing_with_create_ack_does_not_poison_next_replay(self):
        """创建流水已准备时取消，补写确认作废，下一任务仍能开始。"""
        from ecommerce.infrastructure.tasks.events import EventJournal
        job=self.submit();row=self.claim(job)
        append=EventJournal.append
        def cancel_before_append(journal,operation):
            if operation['op']=='create' and operation['task_id']==job:
                self.store.control(job,'stop')
            return append(journal,operation)
        action=unittest.mock.Mock(return_value='不能打开浏览器')
        with patch.object(EventJournal,'append',cancel_before_append):
            result=run_task(self.platform,'collection.taobao.synthetic',action,task_id=job,job_claim_token=row['claim_token'])
        self.assertEqual(result.status,'cancelled');action.assert_not_called()
        self.assertIsNone(self.store.read(job)['task_id'])
        self.assertFalse(EventJournal(self.settings,job).pending())
        next_job=self.submit();next_row=self.claim(next_job)
        result=run_task(self.platform,'collection.taobao.synthetic',lambda:'仅合成动作',task_id=next_job,job_claim_token=next_row['claim_token'])
        self.assertEqual(result.status,'succeeded')

    def test_halt_is_visible_in_database_even_when_event_writer_cannot_advance(self):
        """写入线程被合成故障阻断时，网页诊断仍读到数据库中的中断原因。"""
        from ecommerce.infrastructure.tasks.events import current_events
        from ecommerce.infrastructure.tasks.collection_control import publish
        from ecommerce.shared.exceptions import NeedsAttention
        job=self.submit();row=self.claim(job)
        def action():
            session=current_events();session.stopping.set();session.worker.join(timeout=2)
            session.error=NeedsAttention('合成不可重试持久化错误')
            publish({'halted':True,'message':'合成不可重试持久化错误','allowed':[],'prompt_id':None})
            self.assertTrue(self.store.read(job)['prompt']['halted'])
            self.assertFalse(self.store.read(job)['actual_finished'])
            return '仅验证诊断，不制造采集数据'
        result=run_task(self.platform,'collection.taobao.synthetic',action,task_id=job,job_claim_token=row['claim_token'])
        self.assertEqual(result.status,'needs_attention')

    def test_cli_executes_and_queues_in_same_database_history(self):
        """命令行同步执行和繁忙排队均产生统一申请，繁忙不运行新动作。"""
        from ecommerce.modules.collection.requests import execute as request_execute
        options=self.row()['options']
        result=request_execute(self.platform,'collection.taobao.synthetic',lambda:'合成命令行执行',options,runner=run_task)
        self.ids.append(result.task_id)
        self.assertEqual(result.status,'succeeded')
        row=self.store.read(result.task_id)
        self.assertEqual(row['origin'],'cli');self.assertEqual(row['task_id'],result.task_id)
        self.assertIn(result.task_id,[r['id'] for r in web_collection.history(self.settings)['jobs']])
        active=self.submit();self.claim(active)
        runner=unittest.mock.Mock()
        queued=request_execute(self.platform,'collection.taobao.synthetic',lambda:'不能抢占',
            {**options,'modules':['reviews']},runner=runner)
        self.ids.append(queued.task_id)
        self.assertEqual(queued.status,'queued');runner.assert_not_called()
        self.assertEqual(self.store.read(queued.task_id)['dispatch_state'],'waiting')

    def test_cli_questions_all_keeps_independent_frozen_ranges(self):
        """CLI共享--all转换为独立数量开关，延迟执行和续采参数一致。"""
        from ecommerce.cli.commands.collection import execute_detail
        with patch('ecommerce.modules.collection.detail_service.open_detail_input',return_value='合成问答命令行') as collect:
            result=execute_detail(self.settings,'123',questions=True,all_reviews=True,wait=False)
        self.ids.append(result.task_id)
        self.assertEqual(result.status,'succeeded')
        row=self.store.read(result.task_id)
        self.assertFalse(row['options']['all_reviews']);self.assertTrue(row['options']['all_questions'])
        self.assertFalse(collect.call_args.kwargs['all_reviews']);self.assertTrue(collect.call_args.kwargs['all_questions'])

    def test_launcher_cannot_overwrite_already_bound_worker(self):
        """Windows启动器晚返回时不能把实际工作进程改回启动器PID。"""
        job=self.submit();row=self.store.claim(self.key,job)
        stamp=process_started_at(os.getpid())
        self.assertTrue(self.store.bind_process(job,row['claim_token'],1001,stamp))
        self.assertTrue(self.store.bind_process(job,row['claim_token'],1002,stamp,replace_owner=True))
        self.assertFalse(self.store.bind_process(job,row['claim_token'],1001,stamp))
        self.assertEqual(self.store.read(job)['owner_pid'],1002)

    def test_legacy_queue_enrichment_preserves_provenance_and_rejects_changed_scope(self):
        """补齐旧队列配置时保留原payload，拒绝来源文件改变原商品模块范围。"""
        from ecommerce.infrastructure.storage.time_values import database_now
        row=self.row();j=self.t['collection_jobs']
        legacy={'product_id':'123','module':'video','options':row['options'],'cleanup_tasks':['a'*32],'source_task_id':'a'*32}
        with self.store.connection(True) as c:
            c.execute(j.insert().values(id=row['id'],execution_key=self.key,origin='legacy',dispatch_state='paused',
                payload={**legacy,'legacy_queue':legacy,'migration_pending':True},revision=1,created_at=database_now(),updated_at=database_now()))
        row.update(database=self.settings.mysql.database,state='queued')
        path=self.settings.workspace/'storage/web_collection_jobs'/f"{row['id']}.json"
        atomic_json(path,{**row,'options':{**row['options'],'all_reviews':True}})
        with self.assertRaises(JobConflict):migrate_dispatch_files(self.settings)
        self.assertEqual(self.store.read(row['id'])['dispatch_state'],'paused')
        atomic_json(path,row)
        report=migrate_dispatch_files(self.settings)
        self.assertEqual(report['enriched'],[row['id']])
        with self.store.connection() as c:
            payload=c.scalar(sa.select(j.c.payload).where(j.c.id==row['id']))
        self.assertEqual(payload['legacy_queue'],legacy)
        self.assertFalse(payload.get('migration_pending',False))

    def test_start_selected_paused_request_preserves_queue_and_other_requests(self):
        """单次启动绑定原申请；不改其他暂停申请或解除资源暂停，重复点击不重复派发。"""
        from types import SimpleNamespace
        self.store.pause(self.key,True,'合成暂停')
        first=self.submit();second=self.submit(module='skus',modules=['skus'],
            options={'mode':'detail','input':'123','modules':['skus'],'all_reviews':False,'all_questions':False})
        with self.store.connection() as c:
            queue_before=dict(c.execute(sa.select(self.t['collection_queue_states']).where(self.t['collection_queue_states'].c.execution_key==self.key)).mappings().one())
        with patch.object(web_jobs,'launch',return_value=SimpleNamespace(pid=os.getpid())) as launch:
            started=dispatch.start_job(self.settings,second)
            repeated=dispatch.start_job(self.settings,second)
        self.assertEqual(started['id'],second);self.assertEqual(repeated['id'],second)
        self.assertFalse(started['can_start']);launch.assert_called_once()
        self.assertEqual(self.store.read(first)['dispatch_state'],'paused')
        with self.store.connection() as c:
            queue_after=dict(c.execute(sa.select(self.t['collection_queue_states']).where(self.t['collection_queue_states'].c.execution_key==self.key)).mappings().one())
        self.assertEqual(queue_before,queue_after)
        self.assertIsNone(self.store.read(second)['task_id'])

    def test_history_page_uses_mysql_filter_before_limit_and_keeps_all_requests(self):
        """超过一页的申请按资源过滤后分页，历史失败项不会被最新20条截掉。"""
        from datetime import datetime
        self.store.pause(self.key,True,'合成分页暂停')
        ids=[self.submit(product_id=str(200000+i),options={'mode':'detail','input':str(200000+i),
            'modules':['video'],'all_reviews':False,'all_questions':False}) for i in range(45)]
        j=self.t['collection_jobs']
        ordered=sorted(ids,reverse=True)
        with self.store.connection(True) as c:
            c.execute(j.update().where(j.c.execution_key==self.key).values(created_at=datetime(2026,10,27,10)))
            c.execute(j.update().where(j.c.id==ordered[-1]).values(dispatch_state='start_failed'))
        pages=[self.store.history_page(self.key,page) for page in (1,2,3)]
        self.assertEqual([len(page['jobs']) for page in pages],[20,20,5])
        self.assertEqual([row['id'] for page in pages for row in page['jobs']],ordered)
        failure=self.store.history_page(self.key,1,'failed')
        self.assertEqual(failure['total'],1);self.assertEqual(failure['jobs'][0]['id'],ordered[-1])
        waiting=self.store.history_page(self.key,999,'waiting')
        self.assertEqual((waiting['page'],waiting['pages'],waiting['total']),(3,3,44))
        self.assertTrue(self.store.queue_state(self.key)['paused'])
        self.assertTrue(all(row['task_id'] is None for page in pages for row in page['jobs']))

    def test_history_mysql_json_prompt_and_actual_finished_task_take_precedence(self):
        """MySQL原生JSON提示归人工处理，已结束执行不受旧提示误导。"""
        from datetime import datetime
        self.store.pause(self.key,True,'合成状态验证')
        ids=[self.submit(product_id=str(300000+i),options={'mode':'detail','input':str(300000+i),
            'modules':['video'],'all_reviews':False,'all_questions':False}) for i in range(5)]
        j,t=self.t['collection_jobs'],self.t['tasks']
        with self.store.connection(True) as c:
            for identifier,status in zip(ids[:3],('cancelled','failed','succeeded')):
                c.execute(t.insert().values(id=identifier,name='collection.taobao.synthetic',origin='live',
                    execution_key=self.key,status=status,started_at=datetime(2026,10,27,10),finished_at=datetime(2026,10,27,11)))
                c.execute(j.update().where(j.c.id==identifier).values(dispatch_state='started',task_id=identifier,
                    prompt={'prompt_id':'old-prompt','storage_pending':True}))
            c.execute(j.update().where(j.c.id==ids[3]).values(dispatch_state='claimed',prompt={'storage_pending':True}))
            c.execute(j.update().where(j.c.id==ids[4]).values(dispatch_state='claimed',prompt=None))
        for identifier,status in zip(ids,('stopped','failed','completed','attention','running')):
            with self.subTest(status=status):
                data=self.store.history_page(self.key,1,status)
                self.assertEqual(data['total'],1)
                self.assertEqual(data['jobs'][0]['id'],identifier)

    def test_session_save_failure_records_warning_error_and_pauses_queue_without_launch(self):
        """持续登录文件冲突真实入库失败结论及分级日志，执行器暂停后续申请。"""
        import logging
        from unittest.mock import Mock
        from ecommerce.infrastructure.browser import session
        from ecommerce.infrastructure.tasks.events import EventLogHandler
        job=self.submit();row=self.claim(job)
        pending=self.submit(module='skus',modules=['skus'],options={'mode':'detail','input':'123','modules':['skus']})
        path=self.platform.session_path;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'{"cookies":[],"origins":[],"synthetic":"old"}')
        old=path.read_bytes()
        context=Mock(storage_state=Mock(return_value={'cookies':[],'origins':[],'synthetic':'SYNTHETIC_SECRET_NEVER_LOG'}))
        error=PermissionError('synthetic sharing conflict');error.winerror=5
        logger=logging.getLogger(session.__name__);handler=EventLogHandler(self.settings)
        logger.addHandler(handler)
        try:
            def collect(settings,descriptor):
                return run_task(settings.for_platform('taobao'),'collection.taobao.synthetic',
                    lambda:session.save_session(context,self.platform),task_id=job,job_claim_token=row['claim_token'])
            # 仅拦截当前登录文件替换，不影响任务流水和停止凭据的原子写入。
            original=Path.replace
            def replace_file(source,destination):
                if Path(destination)==path:raise error
                return original(source,destination)
            with patch.object(Path,'replace',replace_file),patch.object(session.time,'sleep'),patch(
                    'ecommerce.modules.collection.web_worker.run_collection',side_effect=collect),patch.object(web_jobs,'launch') as launch:
                execute(self.settings.workspace,job,row['claim_token'],bootstrap=self.settings)
            launch.assert_not_called()
        finally:
            logger.removeHandler(handler);handler.close()
        actual=self.store.read(job)
        self.assertEqual(actual['state'],'failed');self.assertTrue(actual['actual_finished'])
        self.assertIn('登录状态保存失败',actual['message']);self.assertIn('WinError 5',actual['message'])
        self.assertTrue(self.store.queue_state(self.key)['paused'])
        self.assertEqual(self.store.read(pending)['dispatch_state'],'paused')
        self.assertEqual(path.read_bytes(),old);context.storage_state.assert_called_once_with()
        e=self.t['task_events']
        with self.store.connection() as c:
            logs=c.execute(sa.select(e.c.level,e.c.message).where(e.c.task_id==job,e.c.logger_name==session.__name__).order_by(e.c.task_sequence)).all()
        self.assertEqual([log.level for log in logs],['WARNING','WARNING','ERROR'])
        self.assertNotIn('SYNTHETIC_SECRET_NEVER_LOG','\n'.join(log.message for log in logs))

    def test_session_save_retry_success_keeps_task_successful_and_does_not_pause_queue(self):
        """短暂文件冲突恢复后正常完成，警告入库且不误记为采集失败。"""
        import logging
        from unittest.mock import Mock
        from ecommerce.infrastructure.browser import session
        from ecommerce.infrastructure.tasks.events import EventLogHandler
        job=self.submit();row=self.claim(job)
        context=Mock(storage_state=Mock(return_value={'cookies':[],'origins':[]}))
        path=self.platform.session_path;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('{"cookies":[],"origins":[]}',encoding='utf-8')
        original=Path.replace;attempts=[]
        def replace_file(source,destination):
            if Path(destination)==path:
                attempts.append(source)
                if len(attempts)==1:
                    error=PermissionError('synthetic sharing conflict');error.winerror=32;raise error
            return original(source,destination)
        logger=logging.getLogger(session.__name__);handler=EventLogHandler(self.settings)
        logger.addHandler(handler)
        try:
            with patch.object(Path,'replace',replace_file),patch.object(session.time,'sleep'):
                result=run_task(self.platform,'collection.taobao.synthetic',lambda:session.save_session(context,self.platform),
                    task_id=job,job_claim_token=row['claim_token'])
        finally:
            logger.removeHandler(handler);handler.close()
        self.assertEqual(result.status,'succeeded');self.assertEqual(len(attempts),2)
        self.assertFalse(self.store.queue_state(self.key)['paused']);context.storage_state.assert_called_once_with()
        e=self.t['task_events']
        with self.store.connection() as c:
            levels=list(c.scalars(sa.select(e.c.level).where(e.c.task_id==job,e.c.logger_name==session.__name__)))
        self.assertEqual(levels,['WARNING'])
