"""商品补采所有未完成模块的范围、互斥、重核对与结果验证；无真实采集。"""
from unittest.mock import patch
import unittest
from ecommerce.infrastructure.tasks import web_jobs as jobs
from ecommerce.infrastructure.tasks.runner import TaskResult
from ecommerce.modules.collection import web_collection as service
from ecommerce.modules.collection.web_worker import execute
from ecommerce.shared.exceptions import TaskBusy
from tests.unit import test_web_collection as fixtures


class RemainingCollectionTests(unittest.TestCase):
    query=fixtures.WebCollectionTests.query

    def setUp(self):
        """复用临时配置和派发替身；参数：无；返回：无。"""
        fixtures.WebCollectionTests.setUp(self)

    def complete(self,module,identifier=7,status='complete',task='c'*32):
        """登记有明确有效指针的样例；参数：模块、批次、状态和任务；返回：无。"""
        self.data['history'].insert(0,{'id':identifier,'module':module,'status':status,'task_id':task})
        self.data['current_ids'].append(identifier)

    def start(self):
        """派发补齐任务；参数：无；返回：公开状态。"""
        return service.start_remaining(self.settings,'123')

    def test_plan_skips_effective_complete_and_confirmed_empty_not_last_failure(self):
        self.complete('basic_info',1)
        self.complete('video',2,'no_video')
        self.complete('reviews',3,'no_reviews')
        self.data['history'].insert(0,{'id':4,'module':'basic_info','status':'failed','task_id':'d'*32})
        self.data['history'].insert(0,{'id':5,'module':'skus','status':'partial','task_id':'d'*32})
        self.data['history'].insert(0,{'id':6,'module':'main_images','status':'complete','task_id':'d'*32})
        plan=service.plan(self.settings,'123')
        self.assertEqual(plan['skipped_modules'],['basic_info','video','reviews'])
        self.assertIn('skus',plan['modules']);self.assertIn('main_images',plan['modules'])
        self.assertEqual(plan['modules'],[name for name in service.MODULES if name not in plan['skipped_modules']])

    def test_dispatch_freezes_server_plan_and_repeated_click_reuses_task(self):
        self.complete('basic_info')
        job=self.start();self.assertEqual(job['id'],self.start()['id'])
        self.assertEqual(job['mode'],'remaining');self.assertEqual(job['total_count'],8)
        self.assertEqual(job['skipped_modules'],['basic_info']);self.assertNotIn('basic_info',job['modules'])
        self.launch.assert_called_once()
        with self.assertRaises(TaskBusy):service.start(self.settings,'123','skus')

    def test_existing_single_task_blocks_bulk_dispatch(self):
        service.start(self.settings,'123','video')
        with self.assertRaises(TaskBusy):self.start()
        self.launch.assert_called_once()

    def test_all_complete_never_spawns_or_creates_dispatch(self):
        for index,module in enumerate(service.MODULES):self.complete(module,index+1)
        with self.assertRaisesRegex(ValueError,'无需采集'):self.start()
        self.launch.assert_not_called();self.assertEqual(jobs.rows(self.settings),[])
        self.assertEqual(service.plan(self.settings,'123')['modules'],[])

    def test_remaining_api_uses_server_plan_and_rejects_arbitrary_module_lists(self):
        self.complete('video',status='no_video')
        url='/api/collections/products/123/jobs'
        self.assertEqual(self.client.post(url,json={'mode':'remaining'},headers=self.headers).status_code,202)
        response=self.client.get(url).get_json()
        self.assertNotIn('video',response['plan']['modules'])
        self.assertEqual(self.client.post(url,json={'mode':'remaining','modules':['video']},headers=self.headers).status_code,409)
        self.assertEqual(self.client.post(url,json={'mode':'all'},headers=self.headers).status_code,409)

    def test_worker_rechecks_plan_inside_task_lock_and_uses_one_browser(self):
        self.complete('basic_info',1)
        job=self.start();row=jobs.read(self.settings,job['id'])
        self.complete('video',2,'no_video')
        def runner(settings,name,action,**kwargs):
            self.assertEqual(kwargs['task_id'],job['id']);return action()
        with patch('ecommerce.infrastructure.tasks.runner.run_task',side_effect=runner) as task,patch(
                'ecommerce.modules.collection.detail_service.open_detail_input',return_value='本地完成') as browser:
            self.assertEqual(service.run_collection(self.settings,row),'本地完成')
        task.assert_called_once();browser.assert_called_once()
        flags=browser.call_args.kwargs
        self.assertFalse(flags['basic_info']);self.assertFalse(flags['video'])
        self.assertEqual(sum(bool(flags[name]) for name in service.MODULES),7)
        self.assertNotIn('all_reviews',flags)
        self.assertIn('on_module_event',flags)
        self.assertEqual(jobs.read(self.settings,job['id'])['skipped_modules'],['basic_info','video'])

    def test_all_completed_while_queued_does_not_open_browser(self):
        job=self.start();row=jobs.read(self.settings,job['id'])
        for index,module in enumerate(service.MODULES):self.complete(module,index+1)
        with patch('ecommerce.infrastructure.tasks.runner.run_task',side_effect=lambda settings,name,action,**kw:action()),patch(
                'ecommerce.modules.collection.detail_service.open_detail_input') as browser:
            self.assertIn('未打开浏览器',service.run_collection(self.settings,row));browser.assert_not_called()
        self.assertEqual(row['modules'],[])
        self.assertTrue(service.saved_result(self.settings,row)['valid'])

    def test_progress_and_cancel_keep_saved_results_and_next_click_skips_them(self):
        self.complete('basic_info',1)
        job=self.start();row=jobs.read(self.settings,job['id']);row['state']='running'
        row['progress']={'current_module':None,'started_modules':[],'results':[]};jobs.save(self.settings,row)
        service.module_progress(self.settings,row,'video','running')
        self.assertEqual(service.status(self.settings,'123',job['id'])['current_module'],'video')
        self.complete('video',9,'no_video',job['id'])
        service.module_progress(self.settings,row,'video','finished')
        progress=service.status(self.settings,'123',job['id'])
        self.assertEqual(progress['completed_count'],1)
        self.assertEqual(progress['results'][0]['batch_id'],9)
        row.update(state='cancelled',result=service.saved_result(self.settings,row));jobs.save(self.settings,row)
        restored=service.recent(self.settings,'123')[0]
        self.assertTrue(restored['done']);self.assertEqual(restored['completed_count'],1)
        new=self.start()
        self.assertNotEqual(new['id'],job['id']);self.assertNotIn('video',new['modules'])

    def test_saved_results_require_every_selected_module_of_this_task(self):
        job=self.start();row=jobs.read(self.settings,job['id']);row['modules']=['video','questions']
        self.complete('video',8,'no_video',job['id'])
        self.complete('questions',9,'no_questions','e'*32)
        result=service.saved_result(self.settings,row)
        self.assertFalse(result['valid']);self.assertEqual(result['modules'][0]['batch_id'],8)
        self.assertIsNone(result['modules'][1]['batch_id'])

    def test_worker_terminal_results_have_per_module_links_without_old_observation(self):
        job=self.start()
        for index,module in enumerate(job['modules']):self.complete(module,index+1,'complete',job['id'])
        with patch('ecommerce.modules.collection.web_worker.run_collection',return_value=TaskResult(job['id'],'succeeded','模拟已保存',0)):
            execute(self.settings.workspace,job['id'])
        state=self.client.get(f"/api/collections/products/123/jobs/{job['id']}").get_json()
        self.assertEqual(state['state'],'succeeded');self.assertTrue(state['valid'])
        self.assertEqual(len(state['results']),9)
        self.assertTrue(all('batch=' in value['result_url'] and 'observation' not in value['result_url'] for value in state['results']))


if __name__=='__main__':unittest.main()
