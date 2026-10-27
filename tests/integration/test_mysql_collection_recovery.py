"""独立验证库的真实事务、检查点、防回流及清理故障链；不访问淘宝。"""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock
from contextlib import ExitStack, nullcontext
from uuid import uuid4
import hashlib
import json
import os
import tempfile
import unittest
import sqlalchemy as sa

from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.storage.collection_recovery import RecoveryStore, apply_checkpoint
from ecommerce.infrastructure.storage.collection_store import CollectionStore, CollectionWriter
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.modules.collection.database_records import build_snapshot
from ecommerce.modules.collection.recovery import register_attempt
from ecommerce.infrastructure.storage.mysql import MySQLStorageError


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','须显式启用独立验证库')
class MySQLCollectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=replace(load_settings(root=Path(temp.name)),task_backend='mysql',collection_backend='mysql')
        self.assertTrue(self.settings.mysql.database.endswith('_verify'))
        self.store=RecoveryStore(self.settings);self.store.tasks.initialize()
        self.t=self.store.tables
        self.root,self.child=uuid4().hex,uuid4().hex
        self.product=str(int(uuid4().hex[:14],16))
        self.batch=self.root+'-'+uuid4().hex[:8]
        self.asset_ids=set()
        self.extra_tasks=[]
        self.directory=self.settings.workspace/'output/raw/taobao_details'/self.product/'history'/self.batch
        self.addCleanup(self.clean_database)
        with self.store.engine.begin() as c:
            for identifier in (self.root,self.child):
                c.execute(self.t['tasks'].insert().values(id=identifier,name='synthetic.recovery',status='running' if identifier==self.child else 'cancelled',
                    origin='live',started_at=database_now(),finished_at=None if identifier==self.child else database_now()))

    def clean_database(self):
        """只删除本例明确生成的记录，验证库其他资料保持原样。"""
        with self.store.engine.begin() as c:
            task_ids=[self.root,self.child,*self.extra_tasks]
            c.execute(self.t['collection_job_controls'].delete().where(self.t['collection_job_controls'].c.job_id.in_(sa.select(self.t['collection_jobs'].c.id).where(self.t['collection_jobs'].c.payload['source_task_id'].as_string()==self.root))))
            c.execute(self.t['collection_jobs'].delete().where(self.t['collection_jobs'].c.payload['source_task_id'].as_string()==self.root))
            c.execute(self.t['collection_queue_states'].delete().where(self.t['collection_queue_states'].c.execution_key=='e'*64))
            c.execute(self.t['collection_discarded_batches'].delete().where(self.t['collection_discarded_batches'].c.source_task_id.in_([self.root,self.child])))
            c.execute(self.t['collection_plan_executions'].delete().where(self.t['collection_plan_executions'].c.task_id.in_(task_ids)))
            c.execute(self.t['collection_plans'].delete().where(self.t['collection_plans'].c.id.in_(task_ids)))
            ids=list(c.scalars(sa.select(self.t['collection_batches'].c.id).where(self.t['collection_batches'].c.task_id.in_(task_ids))))
            assets=list(c.scalars(sa.select(self.t['media_references'].c.file_asset_id).where(self.t['media_references'].c.batch_id.in_(ids),self.t['media_references'].c.file_asset_id.is_not(None))))
            assets+=list(c.scalars(sa.select(self.t['media_references'].c.file_asset_id).where(self.t['media_references'].c.task_id.in_(task_ids),self.t['media_references'].c.file_asset_id.is_not(None))))
            from ecommerce.infrastructure.storage.collection_store import PRUNE_ORDER
            c.execute(self.t['task_events'].delete().where(self.t['task_events'].c.task_id.in_(task_ids)))
            c.execute(self.t['product_current_sections'].delete().where(self.t['product_current_sections'].c.batch_id.in_(ids)))
            for name in PRUNE_ORDER:
                c.execute(self.t[name].delete().where(self.t[name].c.batch_id.in_(ids)))
            c.execute(self.t['media_references'].delete().where(self.t['media_references'].c.task_id.in_(task_ids)))
            c.execute(self.t['collection_batches'].update().where(self.t['collection_batches'].c.id.in_(ids)).values(step_id=None,run_id=None,derived_from_batch_id=None))
            c.execute(self.t['collection_batches'].delete().where(self.t['collection_batches'].c.id.in_(ids)))
            run_ids=list(c.scalars(sa.select(self.t['collection_runs'].c.id).where(self.t['collection_runs'].c.task_id.in_(task_ids))))
            c.execute(self.t['collection_steps'].delete().where(self.t['collection_steps'].c.run_id.in_(run_ids)))
            c.execute(self.t['collection_runs'].delete().where(self.t['collection_runs'].c.id.in_(run_ids)))
            c.execute(self.t['search_records'].delete().where(self.t['search_records'].c.task_id.in_(task_ids)))
            c.execute(self.t['task_events'].delete().where(self.t['task_events'].c.task_id.in_(task_ids)))
            c.execute(self.t['products'].delete().where(self.t['products'].c.platform_product_id==self.product))
            c.execute(self.t['file_assets'].delete().where(self.t['file_assets'].c.id.in_(set(assets)|self.asset_ids)))
            c.execute(self.t['tasks'].delete().where(self.t['tasks'].c.id.in_(task_ids)))

    def source(self):
        """创建一个未完成SKU批次及独占文件；参数：无；返回：原件和规范快照。"""
        register_attempt(self.settings,self.product,'skus',self.batch,self.root)
        image=self.directory/'sku图/1.jpg';image.parent.mkdir();image.write_bytes(b'synthetic-old-image')
        record={'schema_version':3,'product_id':self.product,'task_id':self.root,'batch_id':self.batch,
            'status':'partial','scope':'product_skus','captured_at':'2026-10-26T12:00:00+08:00',
            'source_url':'https://item.taobao.com/item.htm?id='+self.product,'groups':[], 'candidate_count':1,'processed_count':1,
            'skus':[{'index':1,'name':'合成规格','properties':[],'combination_confirmed':True,'price_value':'1.25',
                'price_status':'collected','stock_status':'selectable','image_status':'downloaded',
                'image':{'index':1,'image_file':'sku图/1.jpg','image_path':str(image),'image_url':'https://example.invalid/synthetic.jpg',
                    'size_bytes':image.stat().st_size,'sha256':hashlib.sha256(image.read_bytes()).hexdigest(),'content_type':'image/jpeg','width':1,'height':1}}]}
        path=self.directory/'sku_details.json';atomic_json(path,record)
        reference=build_snapshot(self.settings,'batch',record,path,module='skus')
        with self.store.engine.begin() as c:
            CollectionWriter(self.settings,c).apply(reference,{'sections':[],'last_attempt':True})
            self.asset_ids.update(c.scalars(sa.select(self.t['file_assets'].c.id).where(self.t['file_assets'].c.relative_path.like('%'+self.batch+'%'))))
        return path,reference

    def checkpoint(self,task_id,revision=1,parent=None):
        return {'id':self.root,'options':{'mode':'detail','input':self.product,'modules':['skus'],'all_reviews':False,'all_questions':False},
                'parent_task_id':parent,'revision':revision,'checkpoint':{'pages':{},'products':{},'finished':False}}

    def test_plan_claim_is_idempotent_and_old_or_changed_scope_cannot_overwrite(self):
        with self.store.engine.begin() as c:
            apply_checkpoint(c,self.t,self.checkpoint(self.root),self.root)
            apply_checkpoint(c,self.t,self.checkpoint(self.root),self.root)
            value=self.checkpoint(self.child,2,self.root)
            apply_checkpoint(c,self.t,value,self.child)
        self.assertEqual(self.store.read(self.child)['active_task_id'],self.child)
        with self.assertRaises(MySQLStorageError),self.store.engine.begin() as c:
            apply_checkpoint(c,self.t,self.checkpoint(self.root,3),self.root)
        with self.assertRaises(MySQLStorageError),self.store.engine.begin() as c:
            value=self.checkpoint(self.child,3,self.root);value['options']['all_reviews']=True
            apply_checkpoint(c,self.t,value,self.child)

    def test_cleanup_deletes_rows_and_files_without_updating_old_half_results(self):
        path,_=self.source()
        source=self.store.batches([self.root],self.product,'skus')[0]
        counts=self.store.cleanup(self.child,source,self.product,'skus')
        self.assertEqual(counts['sku_observations'],1)
        self.assertFalse(path.exists());self.assertFalse((self.directory/'sku图/1.jpg').exists())
        with self.store.engine.connect() as c:
            status=c.scalar(sa.select(self.t['collection_batches'].c.status).where(self.t['collection_batches'].c.source_batch_key==self.batch))
            self.assertEqual(status,'discarded')
            self.assertEqual(c.scalar(sa.select(sa.func.count()).select_from(self.t['sku_observations'])),0)
        self.assertEqual(self.store.batches([self.root],self.product,'skus'),[])
        self.assertEqual(self.store.cleanup(self.child,source,self.product,'skus'),counts)

    def test_file_deletion_failure_is_resumable_after_rows_were_deleted(self):
        path,_=self.source();source=self.store.batches([self.root],self.product,'skus')[0]
        original=Path.unlink
        def locked(target,*args,**kwargs):
            if target==path:raise PermissionError('合成文件占用')
            return original(target,*args,**kwargs)
        with patch.object(Path,'unlink',locked),self.assertRaises(PermissionError):
            self.store.cleanup(self.child,source,self.product,'skus')
        with self.store.engine.connect() as c:
            self.assertEqual(c.scalar(sa.select(self.t['collection_discarded_batches'].c.stage).where(self.t['collection_discarded_batches'].c.source_batch_key==self.batch)),'records_deleted')
        self.store.cleanup(self.child,source,self.product,'skus')
        self.assertFalse(path.exists())

    def test_discarded_old_snapshot_is_acknowledged_without_recreating_observations(self):
        _,reference=self.source();source=self.store.batches([self.root],self.product,'skus')[0]
        self.store.cleanup(self.child,source,self.product,'skus')
        operation={'database':self.settings.mysql.database,'task_id':self.root,'event_key':uuid4().hex,
            'time':'2026-10-26T12:30:00+08:00','snapshot':reference,'context':{}}
        CollectionStore(self.settings).apply_operation(operation)
        with self.store.engine.connect() as c:
            self.assertEqual(c.scalar(sa.select(sa.func.count()).select_from(self.t['sku_observations'])),0)

    def test_shared_file_is_preserved_while_old_detail_rows_are_removed(self):
        _,_=self.source()
        with self.store.engine.begin() as c:
            ref=c.execute(sa.select(self.t['media_references']).where(self.t['media_references'].c.task_id==self.root,self.t['media_references'].c.file_asset_id.is_not(None))).mappings().first()
            c.execute(self.t['media_references'].insert().values(reference_key=uuid4().hex,task_id=self.child,
                role='diagnostic_shared',position=1,file_asset_id=ref['file_asset_id'],download_status='downloaded'))
        try:
            counts=self.store.cleanup(self.child,self.store.batches([self.root],self.product,'skus')[0],self.product,'skus')
            self.assertEqual(counts['shared'],1)
            self.assertTrue((self.directory/'sku图/1.jpg').is_file())
        finally:
            with self.store.engine.begin() as c:
                c.execute(self.t['media_references'].delete().where(self.t['media_references'].c.task_id==self.child))

    def test_queue_deduplicates_active_requests_and_pauses_after_restart(self):
        from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
        from ecommerce.infrastructure.storage.database import now
        jobs=CollectionJobStore(self.settings)
        key='e'*64
        payload={'id':uuid4().hex,'execution_key':key,'created_at':now(),'source_task_id':self.root,
            'product_id':self.product,'module':'skus','mode':'detail','modules':['skus'],'options':{}}
        identifier=jobs.submit(payload)
        self.assertEqual(jobs.submit({**payload,'id':uuid4().hex}),identifier)
        jobs.pause(key)
        self.assertEqual(jobs.read(identifier)['dispatch_state'],'paused')
        self.assertIsNone(jobs.claim(key))
        jobs.pause(key,False)
        claimed=jobs.claim(key)
        self.assertEqual(claimed['id'],identifier)
        self.assertIsNone(jobs.claim(key))
        jobs.control(identifier,'stop')
        self.assertEqual(jobs.read(identifier)['dispatch_state'],'cancelled')

    def test_unindexed_download_is_identified_by_owner_and_cleaned(self):
        register_attempt(self.settings,self.product,'video',self.batch,self.root)
        path=self.directory/'partial.mp4';path.write_bytes(b'synthetic-uncommitted')
        source=self.store.batches([self.root],self.product,'video')[0]
        self.store.cleanup(self.child,source,self.product,'video')
        self.assertFalse(path.exists())

    def test_full_task_failure_stop_resume_cleanup_and_manual_retry_chain(self):
        """真实执行器/流水/数据库串起故障链，仅页面及平台读取使用替身。"""
        from ecommerce.infrastructure.tasks.runner import run_task,current_task
        from ecommerce.infrastructure.tasks.events import storage_checkpoint
        from ecommerce.modules.collection import detail_service
        from ecommerce.modules.collection.detail_storage import product_directory,save_detail_section
        from ecommerce.modules.collection.detail_video import save_video_result
        from ecommerce.modules.collection.detail_parameters import save_parameter_result
        from ecommerce.platforms.taobao.detail import DetailPage,resolve_detail_input
        from ecommerce.shared.exceptions import TaskStopped
        from ecommerce.infrastructure.storage.database import now
        with self.store.engine.begin() as c:
            c.execute(self.t['tasks'].delete().where(self.t['tasks'].c.id.in_([self.root,self.child])))
        settings=self.settings.for_platform('taobao')
        page=MagicMock(url=resolve_detail_input(self.product).url);page.is_closed.return_value=False
        page.screenshot.side_effect=lambda **kw:Path(kw['path']).write_bytes(b'synthetic-screenshot')
        detail=DetailPage(page,resolve_detail_input(self.product),'本地合成商品')
        calls=[];phase={'value':1}

        def guard():
            value=MagicMock(task_id=current_task()[1],failure=None)
            value.collection_module.side_effect=lambda *a,**kw:nullcontext()
            value.checkpoint.side_effect=lambda *a,**kw:storage_checkpoint('合成检查点')
            def perform(label,action,**kwargs):
                result=action();storage_checkpoint(label);return result
            value.perform.side_effect=perform
            return value

        def base(module):
            task=current_task()[1];batch=task+'-'+uuid4().hex[:8]
            register_attempt(settings,self.product,module,batch,task)
            directory=product_directory(settings,self.product)/'history'/batch
            record={'schema_version':3 if module=='skus' else 1,'product_id':self.product,'task_id':task,'batch_id':batch,
                'captured_at':now(),'source_url':detail.target.canonical_url,'scope':'synthetic_'+module}
            return directory,record

        def video(*args,**kwargs):
            calls.append('video');directory,record=base('video')
            value=save_video_result(directory,{**record,'video_status':'no_video','video_present':False,'video_url':None,
                'video_path':None,'video_file':None,'video_sha256':None,'video_size_bytes':0})
            return {**value,'message':'合成无视频'}

        def skus(*args,**kwargs):
            calls.append('skus');directory,record=base('skus')
            record.update(status='complete' if phase['value']==3 else 'partial',candidate_count=1,processed_count=1,groups=[],warnings=[],blocking_issues=[],
                skus=[{'index':1,'name':'合成规格','properties':[],'combination_confirmed':True,'price_value':'1.25',
                    'price_status':'collected','stock_status':'selectable','image_status':'no_image','image':None}])
            path=directory/'sku_details.json';atomic_json(path,record)
            record['history_record_file']=path.relative_to(directory.parent.parent).as_posix()
            atomic_json(path,record)
            save_detail_section(directory.parent.parent,self.product,'sku_details' if phase['value']==3 else 'sku_details_last_attempt',record)
            return {**record,'message':'合成SKU结果','history_record_path':str(path)}

        def parameters(*args,**kwargs):
            calls.append('parameters');directory,record=base('parameters')
            value=save_parameter_result(directory,{**record,'status':'no_parameters','items':[],'summary':{'total_count':0,'read_count':0}},'')
            return {**value,'message':'合成无参数'}

        def questions(*args,**kwargs):
            calls.append('questions')
            if phase['value']==1:
                directory,record=base('questions')
                atomic_json(directory/'questions-progress.json',{**record,'status':'running'})
                raise TaskStopped('合成用户停止')
            return {'status':'skipped','stop_reason':'entry_not_found','message':'合成入口未找到',
                'warnings':[{'code':'questions_entry_not_found','message':'合成入口未找到'}]}

        with ExitStack() as stack:
            browser=stack.enter_context(patch.object(detail_service,'browser_session'))
            browser.return_value.__enter__.return_value=(MagicMock(),page)
            for name,value in [('authenticate',page),('open_detail',detail),('inspect_detail_entry',detail)]:
                stack.enter_context(patch.object(detail_service,name,return_value=value))
            stack.enter_context(patch.object(detail_service,'save_session'))
            stack.enter_context(patch.object(detail_service,'active_guard',side_effect=guard))
            stack.enter_context(patch('ecommerce.platforms.taobao.detail.recover_detail_page',return_value=detail))
            for module,function,action in [('detail_video','collect_main_video',video),('detail_skus','collect_skus',skus),
                ('detail_parameters','collect_parameters',parameters),('detail_questions','collect_questions',questions)]:
                stack.enter_context(patch('ecommerce.modules.collection.'+module+'.'+function,side_effect=action))
            first=run_task(settings,'synthetic.collection_flow',lambda:detail_service.open_detail_input(settings,self.product,
                video=True,skus=True,parameters=True,questions=True,all_questions=True,wait=False),task_id=self.root)
            self.assertEqual(first.status,'cancelled')
            self.assertEqual(calls,['video','skus','parameters','questions'])
            old=self.store.read(self.root)
            self.assertEqual(old['checkpoint']['products'][self.product]['skus']['state'],'failed_skipped')
            self.assertEqual(old['checkpoint']['products'][self.product]['questions']['state'],'running')
            phase['value']=2;calls.clear()
            second=run_task(settings,'synthetic.collection_resume',lambda:detail_service.open_detail_input(settings,self.product,
                video=True,skus=True,parameters=True,questions=True,all_questions=True,wait=False,resume_task_id=self.root),task_id=self.child)
            self.assertEqual(second.status,'succeeded')
            self.assertEqual(calls,['questions'])
            latest=self.store.read(self.child)
            self.assertTrue(latest['checkpoint']['finished'])
            self.assertEqual(latest['checkpoint']['counts']['failed'],1)
            self.assertEqual(latest['checkpoint']['counts']['no_entry'],1)
            phase['value']=3;calls.clear();third=uuid4().hex;self.extra_tasks.append(third)
            result=run_task(settings,'synthetic.collection_manual_retry',lambda:detail_service.open_detail_input(settings,self.product,
                skus=True,wait=False,cleanup_tasks=[self.root,self.child]),task_id=third)
            self.assertEqual(result.status,'succeeded')
            self.assertEqual(calls,['skus'])
            with self.store.engine.connect() as c:
                states=list(c.scalars(sa.select(self.t['collection_batches'].c.status).where(self.t['collection_batches'].c.module=='skus',self.t['collection_batches'].c.task_id.in_([self.root,third]))))
                self.assertEqual(sorted(states),['complete','discarded'])
            original=self.store.read(self.child)
            resolved=original['checkpoint']['products'][self.product]['skus']
            self.assertEqual(resolved['state'],'failed_skipped')
            self.assertEqual(resolved['resolved_by_task_id'],third)
            self.assertEqual(original['checkpoint']['counts']['failed'],0)


if __name__=='__main__':unittest.main()
