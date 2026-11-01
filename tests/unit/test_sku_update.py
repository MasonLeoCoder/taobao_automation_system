"""SKU更新的范围、幂等、在途复用和新旧批次状态隔离。"""
from copy import deepcopy
from datetime import datetime
from unittest.mock import patch
import unittest

from ecommerce.modules.collection import web_collection as service
from ecommerce.modules.collection.sku_update import build
from ecommerce.infrastructure.tasks import web_jobs as jobs
from tests.unit import test_web_collection as fixtures


def batch(identifier=10,status='complete',task='a'*32,at='2026-10-25T12:00:00+08:00',count=5):
    """创建只读模型批次；参数：来源字段；返回：脱敏字典。"""
    return {'id':identifier,'module':'skus','status':status,'captured_at':at,'task_id':task,'record_counts':{'records':count}}


class SkuUpdateModelTests(unittest.TestCase):
    def setUp(self):
        """准备已有有效批次；参数：无；返回：无。"""
        self.data={'product':{'platform_product_id':'123'},'current_ids':[10],'history':[batch()]}

    def test_active_single_and_bulk_take_priority_and_other_product_does_not(self):
        """单模块/多模块在途申请优先，其他商品不混入；参数：无；返回：断言。"""
        job={'id':'b'*32,'task_id':None,'product_id':'123','module':'selected','modules':['video','skus'],
             'state':'queued','done':False,'created_at':'2026-11-01T00:00:00+08:00','message':'已排队'}
        original=deepcopy(self.data);model=build(self.data,[job])
        self.assertTrue(model['active']);self.assertFalse(model['outcome']['complete']);self.assertIsNone(model['outcome']['task_id'])
        self.assertEqual(model['latest_valid']['id'],10);self.assertEqual(original,self.data)
        self.assertFalse(build(self.data,[{**job,'product_id':'456'}])['active'])

    def test_partial_never_claims_full_success_or_replaces_effective_batch(self):
        """任务成功标记不能把部分SKU结果变成完整；参数：无；返回：断言。"""
        self.data['history'].insert(0,batch(11,'partial','b'*32,'2026-11-01T10:00:00+08:00',2))
        job={'id':'b'*32,'task_id':'b'*32,'product_id':'123','module':'skus','state':'succeeded','done':True,'created_at':'2026-11-01T09:59:00+08:00'}
        model=build(self.data,[job]);self.assertEqual(model['outcome']['label'],'部分完成')
        self.assertFalse(model['outcome']['complete']);self.assertEqual(model['latest_valid']['id'],10)
        self.data['history'][0]['status']='complete';self.data['current_ids']=[11]
        self.assertTrue(build(self.data,[job])['outcome']['complete'])

    def test_no_batch_failure_old_running_and_empty_complete_are_not_success(self):
        """无结果失败、孤立running及零条complete均不误报完成；参数：无；返回：断言。"""
        job={'id':'c'*32,'product_id':'123','module':'skus','state':'failed','done':True,'created_at':'2026-11-01T10:00:00+08:00'}
        self.assertEqual(build(self.data,[job])['outcome']['label'],'更新失败')
        self.data['history'].insert(0,batch(12,'running','d'*32,'2026-11-01T11:00:00+08:00'))
        model=build(self.data);self.assertFalse(model['active']);self.assertEqual(model['outcome']['label'],'结果待核对')
        self.data['history']=[batch(count=0)]
        self.assertFalse(build(self.data)['outcome']['complete'])
        self.assertNotIn('此前有效资料',build(self.data)['outcome']['message'])


class SkuUpdateRequestTests(unittest.TestCase):
    def setUp(self):
        """复用独立队列数据库，派发始终替身；参数：无；返回：无。"""
        self.case=fixtures.WebCollectionTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.settings=self.case.settings
        patcher=patch('ecommerce.modules.collection.dispatch.dispatch_next',return_value=None)
        patcher.start();self.addCleanup(patcher.stop)
        self.url='/api/collections/products/123/sku-update'

    def test_explicit_confirmation_fixed_scope_and_preserved_old_partial(self):
        """只有确认后创建SKU申请，不携带清理旧任务或其他模块；参数：无；返回：断言。"""
        self.case.data['history']=[batch(10,'partial',at='2026-10-25T12:00:00+08:00')]
        original=deepcopy(self.case.data)
        for body in ({},{'confirm':False},{'confirm':1},{'confirm':True,'module':'video'},[]):
            self.assertEqual(self.case.client.post(self.url,json=body,headers=self.case.headers).status_code,409)
        self.assertEqual(jobs.rows(self.settings),[])
        response=self.case.client.post(self.url,json={'confirm':True},headers=self.case.headers)
        self.assertEqual(response.status_code,202);row=jobs.read(self.settings,response.json['id'])
        self.assertEqual(row['modules'],['skus']);self.assertNotIn('cleanup_tasks',row)
        self.assertEqual(row['options']['input'],'123');self.assertFalse(row['options']['all_reviews']);self.assertFalse(row['options']['all_questions'])
        self.assertEqual(original,self.case.data)
        self.assertEqual(self.case.client.post(self.url,json={'confirm':True}).status_code,403)

    def test_multiple_tabs_reuse_active_single_and_bulk_without_new_rows(self):
        """不同提交键复用同商品在途SKU，且不新增任务；参数：无；返回：断言。"""
        first=service.start_sku_update(self.settings,'123',submission_key='a'*32)
        second=service.start_sku_update(self.settings,'123',submission_key='b'*32)
        self.assertEqual(first['id'],second['id']);self.assertEqual(len(jobs.rows(self.settings)),1)
        # 再次确认相同提交键沿用原申请，即使后来已经结束也不重开。
        row=jobs.read(self.settings,first['id']);row['state']='cancelled';self.case.save(row)
        replay=service.start_sku_update(self.settings,'123',submission_key='a'*32)
        self.assertEqual(replay['id'],first['id'])
        bulk=service.start_configured(self.settings,{'mode':'detail','input':'123','modules':['video','skus']})
        update=service.start_sku_update(self.settings,'123',submission_key='c'*32)
        self.assertEqual(bulk['id'],update['id']);self.assertEqual(len(jobs.rows(self.settings)),2)

    def test_other_product_or_unrelated_module_not_reused_and_get_is_readonly(self):
        """其他商品或模块不挡本SKU更新，普通GET契约不增加键；参数：无；返回：断言。"""
        service.start(self.settings,'123','video')
        service.start_sku_update(self.settings,'456')
        update=service.start_sku_update(self.settings,'123')
        self.assertEqual(update['module'],'skus');self.assertEqual(len(jobs.rows(self.settings)),3)
        before=deepcopy(jobs.rows(self.settings))
        plain=self.case.client.get('/api/collections/products/123/jobs').json
        self.assertNotIn('sku_update',plain)
        extra=self.case.client.get('/api/collections/products/123/jobs?sku_update=1').json
        self.assertTrue(extra['sku_update']['active']);self.assertIsNone(extra['sku_update']['outcome']['task_url'])
        self.assertEqual(before,jobs.rows(self.settings))
