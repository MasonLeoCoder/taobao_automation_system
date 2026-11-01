"""九模块更新的范围、独立结果和申请幂等行为；不执行浏览器或正式库写入。"""
import unittest
from unittest.mock import patch
from ecommerce.modules.collection import web_collection as service
from ecommerce.modules.collection.module_update import build, EMPTY
from ecommerce.shared.collection_contract import MODULES
from ecommerce.infrastructure.tasks import web_jobs as jobs
from tests.unit import test_web_collection as fixtures


class ModuleUpdateTests(unittest.TestCase):
    def setUp(self):
        """准备隔离队列；参数：无；返回：无。"""
        self.fixture=fixtures.WebCollectionTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.c=self.fixture;self.settings=self.c.settings
        patcher=patch('ecommerce.modules.collection.dispatch.dispatch_next',return_value=None)
        patcher.start();self.addCleanup(patcher.stop)

    def test_nine_modules_new_terminal_request_and_same_submission_replay(self):
        """九模块只采自身；旧申请终态后新编号另开，同编号仍重放原申请。"""
        for index,module in enumerate(MODULES):
            with self.subTest(module=module):
                first=service.start_module_update(self.settings,'123',module,submission_key=f'{index+1:032x}')
                row=jobs.read(self.settings,first['id'])
                self.assertEqual(row['modules'],[module]);self.assertNotIn('cleanup_tasks',row)
                self.assertNotIn('resume_task_id',row)
                second=service.start_module_update(self.settings,'123',module)
                self.assertEqual(first['id'],second['id'])
                row['state']='cancelled';self.c.save(row)
                replay=service.start_module_update(self.settings,'123',module,submission_key=f'{index+1:032x}')
                self.assertEqual(first['id'],replay['id'])
                fresh=service.start_module_update(self.settings,'123',module)
                self.assertNotEqual(first['id'],fresh['id'])

    def test_scope_conflict_reuses_original_without_mutating_it(self):
        """选择不限量不覆盖已排队的默认范围；原范围通过响应明确返回。"""
        for module,flag in [('reviews','all_reviews'),('questions','all_questions')]:
            first=service.start_module_update(self.settings,'123',module)
            second=service.start_module_update(self.settings,'123',module,unlimited=True)
            self.assertEqual(first['id'],second['id']);self.assertFalse(second['options'][flag])
            row=jobs.read(self.settings,first['id']);row['state']='cancelled';self.c.save(row)
            new=service.start_module_update(self.settings,'123',module,unlimited=True)
            self.assertTrue(new['options'][flag]);self.assertNotEqual(first['id'],new['id'])

    def test_confirmation_validation_and_readonly_lookup(self):
        """严格校验确认与范围，超时核查只读且绑定商品模块。"""
        url='/api/collections/products/123/module-update/questions'
        for body in ({},{'confirm':1},{'confirm':True,'unlimited':1},{'confirm':True,'other':1}):
            self.assertEqual(self.c.client.post(url,json=body,headers=self.c.headers).status_code,409)
        self.assertEqual(len(jobs.rows(self.settings)),0)
        headers={**self.c.headers,'X-Submission-Id':'a'*32}
        first=self.c.client.post(url,json={'confirm':True,'unlimited':True},headers=headers)
        self.assertEqual(first.status_code,202)
        checked=self.c.client.get(url+'?submission_id='+'a'*32)
        self.assertEqual(checked.json['job']['id'],first.json['id'])
        self.assertEqual(len(jobs.rows(self.settings)),1)
        self.assertEqual(self.c.client.get(url+'?submission_id='+'b'*32).json,{'job':None})
        self.assertEqual(self.c.client.post(url,json={'confirm':True}).status_code,403)
        self.assertEqual(self.c.client.post('/api/collections/products/123/module-update/other',json={'confirm':True},headers=headers).status_code,409)

    def test_public_model_empty_partial_and_bulk_failure(self):
        """无内容独立完成；部分结果保留有效资料；多模块失败不污染已完成模块。"""
        for module in MODULES:
            old={'id':1,'module':module,'status':'complete','captured_at':'2026-10-24','task_id':'a'*32,'record_counts':{'records':2}}
            new={**old,'id':2,'status':'partial','captured_at':'2026-11-01','task_id':'b'*32}
            data={'product':{'platform_product_id':'123'},'history':[new,old],'current_ids':[1]}
            model=build(data,[],module)
            self.assertEqual(model['latest_valid']['id'],1);self.assertEqual(model['outcome']['label'],'部分完成')
            new['status']='complete';data['current_ids']=[2]
            job={'id':'b'*32,'task_id':'b'*32,'module':'selected','modules':[module],'product_id':'123','created_at':'2026-10-31','done':True,'state':'failed'}
            self.assertTrue(build(data,[job],module)['outcome']['complete'])
            for empty in EMPTY.get(module,()):
                new.update(status=empty,record_counts={'records':0})
                self.assertEqual(build(data,[],module)['outcome']['label'],'已确认无内容')

    def test_optional_get_does_not_change_default_contract(self):
        """更新快照只在显式选择时读取，普通接口不新增字段或写入。"""
        before=jobs.rows(self.settings)
        plain=self.c.client.get('/api/collections/products/123/jobs').json
        extra=self.c.client.get('/api/collections/products/123/jobs?module_updates=1').json
        self.assertNotIn('module_updates',plain)
        self.assertEqual(set(extra['module_updates']),set(MODULES));self.assertEqual(before,jobs.rows(self.settings))

    def test_worker_forwards_only_selected_module_and_explicit_unlimited(self):
        """实际工作流边界保持单模块，已确认不限量传入采集器且不清理旧结果。"""
        for module in MODULES:
            job=service.start_module_update(self.settings,'123',module,unlimited=module in {'reviews','questions'})
            row=jobs.read(self.settings,job['id']);row['claim_token']='test-only'
            with patch('ecommerce.infrastructure.tasks.runner.run_task',side_effect=lambda settings,name,action,**options:action()),patch(
                'ecommerce.modules.collection.detail_service.open_detail_input',return_value='模拟完成') as collect,patch(
                'ecommerce.infrastructure.tasks.web_jobs.save'):
                self.assertEqual(service.run_collection(self.settings,row),'模拟完成')
            values=collect.call_args.kwargs
            self.assertEqual([m for m in MODULES if values[m]],[module])
            self.assertIsNone(values['cleanup_tasks'])
            if module=='reviews':self.assertTrue(values['all_reviews'])
            elif module=='questions':self.assertTrue(values['all_questions'])
            else:self.assertNotIn('all_reviews',values);self.assertNotIn('all_questions',values)
