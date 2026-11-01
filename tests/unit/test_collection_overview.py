"""九模块采集概况的范围、状态与只读错误边界验证。"""
from dataclasses import replace
from datetime import datetime
import unittest
from unittest.mock import patch
import sqlalchemy as sa
from ecommerce.modules.collection.collection_overview import build,LABELS
from ecommerce.infrastructure.storage.collection_browser import browse
from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from tests.unit import test_collection_browser as fixtures


class OverviewTests(unittest.TestCase):
    def setUp(self):
        """复用隔离资料，不连接正式库；参数：无；返回：无。"""
        self.case=fixtures.CollectionBrowserTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.case.app.config['SETTINGS']=replace(self.case.settings,task_backend='mysql')
        mock=patch('ecommerce.modules.collection.web_collection.recent',return_value=[]);mock.start();self.addCleanup(mock.stop)

    def data(self):
        """读取有概况元信息的脱敏商品；参数：无；返回：资料。"""
        return browse(self.case.settings,'product',product_id='123',with_overview=True)

    def test_nine_modules_default_tab_and_saved_failed_separation(self):
        """独立Tab展示九行，新异常不覆盖旧有效资料；参数：无；返回：断言。"""
        model=build(self.data());rows={r['module']:r for r in model['rows']}
        self.assertEqual(list(rows),list(LABELS));self.assertEqual(model['ready'],1)
        self.assertEqual(model['remaining'],8);self.assertEqual(model['issues'],1)
        self.assertEqual(rows['parameters']['source_batch'],20);self.assertEqual(rows['parameters']['attempt_state'],'partial')
        page=self.case.client.get('/collections/products/123?module=overview')
        self.assertEqual(page.status_code,200);self.assertEqual(page.text.count('data-overview-row='),9)
        self.assertIn('评价概览',page.text);self.assertIn('评价明细',page.text)
        basic=self.case.client.get('/collections/products/123').text
        self.assertIn('pd-basic',basic);self.assertNotIn('data-collection-overview',basic)
        self.assertNotIn('pd-module-details',basic)

    def test_stopped_attempt_without_batch_and_historical_context(self):
        """未保存批次的停止任务也显示，概况不继承搜索运行限制；参数：无；返回：断言。"""
        c=self.case;c.put('tasks',id='b'*32,name='collection.taobao.detail.open',status='cancelled',started_at=c.now,finished_at=c.now)
        c.put('collection_runs',id=80,product_id=1,task_id='b'*32,status='running',started_at=c.now)
        c.put('collection_steps',id=80,run_id=80,module='questions',requested=True,status='running')
        model=build(self.data());row=next(r for r in model['rows'] if r['module']=='questions')
        self.assertEqual(row['attempt_label'],'已停止');self.assertFalse(row['busy']);self.assertTrue(row['issue'])
        page=c.client.get('/collections/products/123?module=overview&observation=999999')
        self.assertEqual(page.status_code,200);self.assertIn('已停止',page.text)
        self.assertEqual(c.client.get('/collections/products/123?module=overview&batch=20').status_code,400)
        self.assertEqual(c.client.get('/collections/products/123?module=overview&overview_filter=bad').status_code,400)

    def test_pending_single_bulk_and_other_product_isolation(self):
        """活动申请阻止重复点击，不生成不存在的任务详情链接；参数：无；返回：断言。"""
        job={'id':'c'*32,'product_id':'123','module':'selected','mode':'detail','modules':['video','questions'],'state':'queued','done':False,'created_at':'2026-11-01T01:00:00+08:00'}
        rows={r['module']:r for r in build(self.data(),[job])['rows']}
        self.assertTrue(rows['video']['busy']);self.assertTrue(rows['questions']['busy']);self.assertFalse(rows['skus']['busy'])
        from ecommerce.web.collection_overview import present
        with self.case.app.test_request_context():
            view=present(build(self.data(),[job]),'123')
        video=next(r for r in view['rows'] if r['module']=='video')
        self.assertIn('/modules/collection',video['active_url']);self.assertEqual(video['active_label'],'查看申请进度')
        self.assertFalse(next(r for r in build(self.data(),[{**job,'product_id':'456'}])['rows'] if r['module']=='video')['busy'])
        failed={**job,'done':True,'state':'failed','message':'尚未进入采集即失败'}
        rows={r['module']:r for r in build(self.data(),[failed])['rows']}
        self.assertTrue(rows['video']['issue']);self.assertFalse(rows['video']['busy'])
        # 同任务已有具体模块完整结果时，其他模块失败不能覆盖它。
        failed.update(module='selected',modules=['parameters'],task_id=self.case.task)
        rows={r['module']:r for r in build(self.data(),[failed])['rows']}
        self.assertEqual(rows['parameters']['attempt_state'],'partial')

    def test_confirmed_no_entry_and_known_missing_media_distinct(self):
        """无入口不是未采，素材缺失不删除已有记录；参数：无；返回：断言。"""
        c=self.case;c.put('collection_runs',id=80,product_id=1,task_id=c.task,status='complete_with_warnings',started_at=c.now)
        c.put('collection_steps',id=80,run_id=80,module='questions',requested=True,status='skipped',payload={'record':{'stop_reason':'entry_not_found'}})
        c.batch(30,'main_images',product_id=1)
        c.put('file_assets',id=30,root_key='workspace',relative_path='output/missing.jpg',availability='missing',size_bytes=0,sha256='a'*64)
        c.put('media_references',id=30,batch_id=30,role='main_image',file_asset_id=30)
        c.put('media_references',id=31,batch_id=30,role='main_image',file_asset_id=None,source_url='https://example.invalid/remote.jpg')
        c.put('product_current_sections',product_id=1,section='main_images',module='main_images',batch_id=30)
        rows={r['module']:r for r in build(self.data())['rows']}
        self.assertEqual(rows['questions']['saved_state'],'已确认无入口');self.assertFalse(rows['questions']['remaining'])
        self.assertIn('1 条素材',rows['main_images']['media_note']);self.assertTrue(rows['main_images']['has_valid'])
        self.assertEqual(rows['main_images']['quantity'],'已保存 2 张图片')
        # 兼容早期直接存于步骤payload的无入口证明，和嵌套record口径一致。
        with c.engine.begin() as connection:
            steps=c.store.tables['collection_steps']
            connection.execute(steps.update().where(steps.c.id==80).values(payload={'stop_reason':'entry_not_found'}))
        legacy=next(r for r in build(self.data())['rows'] if r['module']=='questions')
        self.assertEqual(legacy['attempt_label'],'已确认无入口');self.assertFalse(legacy['issue'])

    def test_read_failure_not_empty_or_collectable_and_polling_metadata(self):
        """初读失败返回明确错误页；正常轮询保持原接口并额外提供九行；参数：无；返回：断言。"""
        with patch('ecommerce.web.routes.pages.browse_query',side_effect=MySQLStorageError('模拟断库')):
            page=self.case.client.get('/collections/products/123?module=overview')
        self.assertEqual(page.status_code,503);self.assertIn('资料状态读取失败',page.text)
        self.assertNotIn('data-overview-collect',page.text)
        api=self.case.client.get('/api/collections/products/123/jobs?overview=1')
        self.assertEqual(api.status_code,200);self.assertEqual(len(api.get_json()['overview']['rows']),9)
        self.assertIn('plan',api.get_json());self.assertIn('run_status',api.get_json())
        filtered=self.case.client.get('/collections/products/123?module=overview&overview_filter=issues').text
        self.assertEqual(filtered.count('data-co-issue="true"'),1)
        import re
        rows=re.findall(r'<article\b[^>]*data-overview-row="([^"]+)"([^>]*)>',filtered)
        self.assertEqual([name for name,attrs in rows if 'hidden' not in attrs],['parameters'])
