"""网页单模块派发、人工反馈和结果核对；全部使用临时目录及模拟采集。"""
from dataclasses import replace
from datetime import datetime,timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock,patch

from ecommerce.infrastructure.tasks import web_jobs as jobs
from ecommerce.infrastructure.tasks.ownership import atomic_json,execution_key
from ecommerce.infrastructure.tasks.runner import TaskResult
from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection import web_collection as service
from ecommerce.modules.collection.web_worker import execute
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import TaskBusy
from ecommerce.web.app import create_app


class WebCollectionTests(unittest.TestCase):
    def setUp(self):
        """准备配置和模拟商品；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.base=load_settings(root=Path(temp.name))
        self.app=create_app(self.base)
        self.settings=replace(self.base,task_backend='mysql',collection_backend='mysql')
        self.app.config['SETTINGS']=self.settings
        self.client=self.app.test_client()
        self.headers={'X-Collection-Token':self.app.config['COLLECTION_TOKEN']}
        self.data={'history':[],'current_ids':[],'product':{'id':1,'platform_product_id':'123'}}
        for target,kwargs in [
            ('ecommerce.modules.collection.web_collection.query',{'side_effect':self.query}),
            ('ecommerce.modules.collection.web_collection.active_local_tasks',{'return_value':[]}),
            ('ecommerce.infrastructure.tasks.web_jobs.alive',{'return_value':True}),
            ('ecommerce.infrastructure.tasks.web_jobs.launch',{'return_value':SimpleNamespace(pid=9999)}),
            ('ecommerce.modules.collection.web_collection.process_started_at',{'return_value':now()})]:
            mock=patch(target,**kwargs);value=mock.start();self.addCleanup(mock.stop)
            if target.endswith('.launch'):self.launch=value

    def query(self,settings,kind,**parameters):
        """提供商品与缺失任务样例；参数：查询条件；返回：模拟数据。"""
        if kind=='browse_task':raise LookupError('无任务')
        return self.data

    def start(self,module='video'):
        """派发模拟模块；参数：模块；返回：公开状态。"""
        return service.start(self.settings,'123',module)

    def prompt(self,job,identifier='b'*32,allowed=None):
        """发布模拟守卫提示；参数：任务、提示和动作；返回：无。"""
        atomic_json(self.settings.workspace/'storage/task_controls'/f"{job['id']}.state.json",
                    {'task_id':job['id'],'prompt_id':identifier,'allowed':allowed or ['继续'],
                     'step':'验证','message':'请完成验证'})

    def test_duplicate_click_reuses_job_and_other_module_is_busy(self):
        first=self.start()
        self.assertEqual(first['id'],self.start()['id']);self.launch.assert_called_once()
        with self.assertRaises(TaskBusy):self.start('skus')

    def test_invalid_module_product_or_backend_never_spawns(self):
        for identifier,module in [('123','all'),('123',[]),('0','video'),('../123','skus')]:
            with self.assertRaises(ValueError):service.start(self.settings,identifier,module)
        with self.assertRaises(ValueError):service.start(self.base,'123','video')
        self.launch.assert_not_called()

    def test_post_token_origin_host_and_get_cannot_launch(self):
        url='/api/collections/products/123/jobs'
        self.assertEqual(self.client.post(url,json={'module':'video'}).status_code,403)
        self.assertEqual(self.client.post(url,json={'module':'video'},headers={**self.headers,'Origin':'https://foreign.invalid'}).status_code,403)
        self.assertEqual(self.client.post(url,json={'module':'video'},headers=self.headers,base_url='http://foreign.invalid').status_code,403)
        self.assertEqual(self.client.get(url).status_code,200)
        self.launch.assert_not_called()
        self.assertEqual(self.client.post(url,json={'module':[]},headers=self.headers).status_code,409)
        self.assertEqual(self.client.post(url,json={'module':'video'},headers=self.headers).status_code,202)

    def test_job_is_bound_to_product_database_and_resource(self):
        job=self.start()
        with self.assertRaises(LookupError):service.status(self.settings,'456',job['id'])
        with self.assertRaises(LookupError):service.status(replace(self.settings,mysql=replace(self.settings.mysql,database='other')),'123',job['id'])
        self.assertEqual(service.recent(self.settings,'123')[0]['id'],job['id'])
        self.assertNotIn('config',service.status(self.settings,'123',job['id']))

    def test_prompt_recovery_is_read_only_and_stale_decision_rejected(self):
        job=self.start();self.prompt(job)
        state=service.status(self.settings,'123',job['id'])
        self.assertEqual(state['prompt']['message'],'请完成验证');self.assertFalse(state['done'])
        with self.assertRaises(ValueError):service.control(self.settings,'123',job['id'],'continue','c'*32)
        with patch('ecommerce.modules.account.taobao_control.request_control',return_value='已发送') as control:
            self.assertEqual(service.control(self.settings,'123',job['id'],'continue','b'*32),'已发送')
            self.assertEqual(control.call_args.kwargs,{'task_id':job['id'],'prompt_id':'b'*32})

    def test_pending_instruction_cannot_be_overwritten(self):
        job=self.start();self.prompt(job)
        atomic_json(self.settings.workspace/'storage/task_controls'/f"{job['id']}.json",{'action':'continue'})
        with self.assertRaises(ValueError):service.control(self.settings,'123',job['id'],'continue','b'*32)
        row=jobs.read(self.settings,job['id']);row['state']='running';jobs.save(self.settings,row)
        with patch('ecommerce.modules.account.taobao_control.request_control',return_value='已停止'):
            self.assertEqual(service.control(self.settings,'123',job['id'],'stop'),'已停止')

    def test_queued_stop_prevents_worker_collection(self):
        job=self.start()
        service.control(self.settings,'123',job['id'],'stop')
        with patch('ecommerce.modules.collection.web_worker.run_collection') as collect:
            execute(self.settings.workspace,job['id']);collect.assert_not_called()
        self.assertEqual(service.status(self.settings,'123',job['id'])['state'],'cancelled')

    def test_spawn_failure_is_persisted_and_can_retry(self):
        self.launch.side_effect=OSError('cannot spawn')
        with self.assertRaises(OSError):self.start()
        self.assertEqual(jobs.rows(self.settings)[0]['state'],'failed')
        self.launch.side_effect=None;self.start();self.assertEqual(self.launch.call_count,2)

    def test_dead_process_is_interrupted_without_replaying(self):
        job=self.start()
        with patch.object(jobs,'alive',return_value=False):
            result=service.status(self.settings,'123',job['id'])
            self.assertTrue(result['done']);self.assertEqual(result['state'],'interrupted')
        self.launch.assert_called_once()

    def test_unknown_process_never_allows_duplicate_relaunch(self):
        job=self.start();row=jobs.read(self.settings,job['id'])
        row['created_at']=(datetime.fromisoformat(now())-timedelta(minutes=2)).isoformat();jobs.save(self.settings,row)
        with patch.object(jobs,'alive',return_value=None):
            result=service.status(self.settings,'123',job['id'])
            self.assertFalse(result['done']);self.assertEqual(result['state'],'unverified')
            self.assertEqual(self.start()['id'],job['id'])
        self.launch.assert_called_once()

    def test_existing_cli_task_blocks_web_dispatch(self):
        with patch.object(service,'active_local_tasks',return_value=[{'id':'a'*32}]):
            with self.assertRaises(TaskBusy):self.start()
        self.launch.assert_not_called()

    def test_only_chosen_module_reaches_shared_collector(self):
        row={'id':'a'*32,'product_id':'123','module':'reviews'}
        def runner(settings,name,action,**options):
            self.assertEqual(options['task_id'],row['id']);return action()
        with patch('ecommerce.infrastructure.tasks.runner.run_task',side_effect=runner),patch(
            'ecommerce.modules.collection.detail_service.open_detail_input',return_value='模拟完成') as collect:
            self.assertEqual(service.run_collection(self.settings,row),'模拟完成')
            values=collect.call_args.kwargs
            self.assertFalse(values['wait']);self.assertTrue(values['reviews'])
            self.assertEqual(sum(bool(values[module]) for module in service.MODULES),1)
            self.assertNotIn('all_reviews',values)

    def test_success_requires_published_current_module_of_exact_task(self):
        job=self.start()
        self.data['history']=[{'id':7,'task_id':job['id'],'module':'video','status':'no_video'}]
        self.data['current_ids']=[7]
        with patch('ecommerce.modules.collection.web_worker.run_collection',return_value=TaskResult(job['id'],'succeeded','已确认无视频',0)):
            execute(self.settings.workspace,job['id'])
        result=service.status(self.settings,'123',job['id'])
        self.assertTrue(result['valid']);self.assertEqual(result['batch_id'],7)
        self.assertEqual(result['result_label'],'查看采集结果');self.assertEqual(result['state'],'succeeded')

    def test_old_success_cannot_substitute_for_missing_current_result(self):
        job=self.start();self.data['history']=[{'id':7,'task_id':'c'*32,'module':'video','status':'downloaded'}];self.data['current_ids']=[7]
        with patch('ecommerce.modules.collection.web_worker.run_collection',return_value=TaskResult(job['id'],'succeeded','已结束',0)):
            execute(self.settings.workspace,job['id'])
        result=service.status(self.settings,'123',job['id'])
        self.assertFalse(result['valid']);self.assertIsNone(result['batch_id']);self.assertEqual(result['state'],'needs_attention')

    def test_partial_result_is_retained_without_claiming_success(self):
        job=self.start();self.data['history']=[{'id':8,'task_id':job['id'],'module':'video','status':'partial'}]
        with patch('ecommerce.modules.collection.web_worker.run_collection',return_value=TaskResult(job['id'],'needs_attention','部分下载',3)):
            execute(self.settings.workspace,job['id'])
        result=service.status(self.settings,'123',job['id'])
        self.assertFalse(result['valid']);self.assertEqual(result['batch_id'],8);self.assertTrue(result['done'])

    def test_result_link_uses_new_batch_without_original_observation(self):
        job=self.start();row=jobs.read(self.settings,job['id']);row.update(state='succeeded',result={'batch_id':7,'valid':True});jobs.save(self.settings,row)
        result=self.client.get(f"/api/collections/products/123/jobs/{job['id']}").get_json()
        self.assertIn('batch=7',result['result_url']);self.assertNotIn('observation',result['result_url'])

    def test_file_only_feedback_has_reason_and_never_reads_stdin(self):
        context,page=MagicMock(),MagicMock();context.pages=[page];page.is_closed.return_value=False
        guard=TaobaoGuard(context,self.settings.log_dir);guard.feedback_file_only=True
        def respond(milliseconds):
            state=json.loads(guard.control_state_path.read_text(encoding='utf-8'))
            self.assertEqual(state['message'],'请处理验证')
            atomic_json(guard.control_path,{'action':'continue','prompt_id':state['prompt_id']})
        page.wait_for_timeout.side_effect=respond
        with patch('builtins.input',side_effect=AssertionError('不允许读终端')):
            self.assertEqual(guard.read_feedback('请处理验证'),'继续')
        self.assertIsNone(guard._feedback_thread)
        self.assertIsNone(json.loads(guard.control_state_path.read_text(encoding='utf-8'))['prompt_id'])

    def test_storage_pending_exposes_stop_but_never_authorizes_continue(self):
        job=self.start();self.prompt(job)
        row=jobs.read(self.settings,job['id']);row['state']='running';jobs.save(self.settings,row)
        active={'id':job['id'],'storage_pending':True}
        with patch.object(service,'active_local_tasks',return_value=[active]),patch(
                'ecommerce.modules.account.taobao_control.request_control',side_effect=ValueError('不能绕过存储暂停')):
            self.assertTrue(service.status(self.settings,'123',job['id'])['storage_pending'])
            with self.assertRaises(ValueError):service.control(self.settings,'123',job['id'],'continue','b'*32)

    def test_login_feedback_rechecks_authentication_instead_of_assuming_success(self):
        from ecommerce.platforms.taobao.login import login_manually
        context,page=MagicMock(),MagicMock();context.pages=[page];page.context=context
        page.is_closed.return_value=False
        feedback=MagicMock(return_value='继续')
        with patch('ecommerce.platforms.taobao.login.active_guard',return_value=None),patch(
                'ecommerce.platforms.taobao.login.checkpoint'),patch(
                'ecommerce.platforms.taobao.login.is_authenticated',side_effect=[False,False,True]):
            self.assertIs(login_manually(page,1000,on_login_required=feedback),page)
        self.assertEqual(feedback.call_count,2)

    def test_native_dialog_uses_explicit_choices_and_file_channel(self):
        guard=MagicMock();guard.read_feedback.return_value='取消'
        with patch('ecommerce.platforms.taobao.verification.active_guard',return_value=guard),patch(
                'ecommerce.infrastructure.tasks.events.current_events',return_value=None),patch(
                'ecommerce.infrastructure.tasks.events.storage_checkpoint'):
            self.assertEqual(service.web_attention('【需要人工决定原生弹窗】是否确认'),'取消')
        self.assertTrue(guard.feedback_file_only)
        self.assertEqual(guard.read_feedback.call_args.kwargs['allowed'],{'确认','取消'})

    def test_crash_after_database_finish_restores_result_without_collection(self):
        job=self.start()
        self.data['history']=[{'id':9,'task_id':job['id'],'module':'video','status':'downloaded'}];self.data['current_ids']=[9]
        def query(settings,kind,**parameters):
            return {'task':{'status':'succeeded','finished_at':now(),'message':'完成'}} if kind=='browse_task' else self.data
        with patch.object(jobs,'alive',return_value=False),patch.object(service,'query',side_effect=query):
            result=service.status(self.settings,'123',job['id'])
        self.assertTrue(result['valid']);self.assertEqual(result['state'],'succeeded');self.assertEqual(result['batch_id'],9)
        self.launch.assert_called_once()

    def test_worker_uses_persisted_configuration_in_separate_process(self):
        job=self.start()
        code='''
import sys
from unittest.mock import patch
from ecommerce.modules.collection.web_worker import execute
from ecommerce.infrastructure.tasks.runner import TaskResult
data={'history':[{'id':71,'task_id':sys.argv[2],'module':'video','status':'no_video'}],'current_ids':[71]}
with patch('ecommerce.modules.collection.web_collection.query',return_value=data),patch('ecommerce.modules.collection.web_worker.run_collection',return_value=TaskResult(sys.argv[2],'succeeded','local fixture only',0)):
    execute(sys.argv[1],sys.argv[2])
'''
        completed=subprocess.run([sys.executable,'-c',code,str(self.settings.workspace),job['id']],
                    capture_output=True,text=True,timeout=20,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.assertEqual(completed.returncode,0,completed.stderr)
        row=jobs.read(self.settings,job['id'])
        self.assertEqual(row['state'],'succeeded');self.assertTrue(row['result']['valid'])
        self.assertEqual(row['result']['batch_id'],71)
        self.assertNotEqual(row['owner_pid'],os.getpid())


if __name__=='__main__':unittest.main()
