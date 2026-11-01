"""基本信息批次卡片的独立来源、默认有效资料与异常记录边界。"""
from datetime import datetime
import unittest
from tests.unit import test_collection_browser as fixtures
from ecommerce.infrastructure.storage.collection_queries import records


class BasicBatchTests(unittest.TestCase):
    def setUp(self):
        """建立不同任务和批次的隔离样例；参数：无；返回：无。"""
        c=self.case=fixtures.CollectionBrowserTests();c.setUp();self.addCleanup(c.doCleanups)
        c.put('tasks',id='b'*32,name='collection.taobao.detail.open',status='succeeded',started_at=c.now)
        for identifier,price,time in [(60,'旧批价格 11元',datetime(2026,10,24)),(70,'当前价格 22元',c.now),(80,'部分价格 33元',datetime(2026,10,26))]:
            c.batch(identifier,'basic_info',product_id=1,task_id='b'*32 if identifier==60 else c.task,
                    status='partial' if identifier==80 else 'complete',captured_at=time,
                    result_payload={'record':{'message':'字段读取未完成 <script>unsafe</script>'}} if identifier==80 else {})
            c.put('product_info_observations',id=identifier,batch_id=identifier,product_id=1,module='basic_info',
                  title='本批标题 '+str(identifier),shop_name='本批店铺',price_text=price,sales_text='已售 10万+',captured_at=time)
        c.put('product_current_sections',product_id=1,section='product_info',module='basic_info',batch_id=70)

    def test_historical_batch_cannot_borrow_current_price_or_task(self):
        """旧批内容及任务固定，不因分页/排序/种类参数切换或混入当前价格；参数：无；返回：断言。"""
        page=self.case.client.get('/collections/products/123?batch=60&page=99&after=999&kind=reviews&sort=content').text
        self.assertIn('历史批次',page);self.assertIn('旧批价格 11元',page);self.assertNotIn('当前价格 22元',page)
        self.assertIn('data-bb-task href="/tasks/'+('b'*32),page)
        self.assertIn('data-bb-logs href="/logs?task_id='+('b'*32),page)
        self.assertNotIn('table-wrap',page);self.assertNotIn('每页 20 条',page)
        self.assertIn('返回基本信息',page)

    def test_default_follows_valid_pointer_despite_newer_partial_and_notes_optional(self):
        """最近失败不冒充当前有效资料，默认读有效批次且原records契约保持；参数：无；返回：断言。"""
        page=self.case.client.get('/collections/products/123?module=basic_info').text
        self.assertIn('当前查看：批次 70',page);self.assertIn('最近有效批次',page)
        self.assertIn('当前价格 22元',page);self.assertNotIn('部分价格 33元',page)
        self.assertIn('value="70" selected',page);self.assertNotIn(' · complete',page)
        self.assertNotIn('batch_note',records(self.case.settings,80))
        self.assertIn('字段读取未完成',records(self.case.settings,80,with_batch_note=True)['batch_note'])

    def test_partial_empty_and_foreign_batches_keep_own_data(self):
        """部分/无明细按本批显示，消息转义，其他商品不可查看；参数：无；返回：断言。"""
        c=self.case;page=c.client.get('/collections/products/123?batch=80').text
        self.assertIn('部分价格 33元',page);self.assertIn('部分完成',page);self.assertIn('采集尝试',page)
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;',page);self.assertNotIn('<script>unsafe</script>',page)
        c.batch(81,'basic_info',product_id=1,status='failed',result_payload={'record':{'error':{'message':'明确失败原因'}}})
        empty=c.client.get('/collections/products/123?batch=81').text
        self.assertIn('明确失败原因',empty);self.assertIn('没有已保存的基本信息明细',empty)
        self.assertNotIn('当前价格 22元',empty)
        c.batch(90,'basic_info',product_id=2)
        self.assertEqual(c.client.get('/collections/products/123?batch=90').status_code,404)
        self.assertEqual(c.client.get('/collections/products/456?module=basic_info').status_code,200)
