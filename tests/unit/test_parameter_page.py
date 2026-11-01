"""参数页批次来源、整批计数、空态及读取失败的隔离检查。"""
from unittest.mock import patch
import unittest
from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from tests.unit import test_collection_browser as fixtures


class ParameterPageTests(unittest.TestCase):
    def setUp(self):
        """复用只读查询隔离库；参数：无；返回：无。"""
        self.case=fixtures.CollectionBrowserTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.client=self.case.client

    def test_total_is_full_batch_and_kind_cannot_borrow_other_module(self):
        """标题统计整批且保持分页，同名保留，不借用外部kind读取其他模块。"""
        for n in range(23):self.case.put('parameter_observations',id=200+n,batch_id=20,position=3+n,name='同名参数',value=str(n))
        first=self.client.get('/collections/products/123?module=parameters&kind=reviews').text
        second=self.client.get('/collections/products/123?module=parameters&page=2').text
        self.assertIn('共 25 项',first);self.assertIn('共 25 项',second)
        self.assertEqual(first.count('<tr data-parameter-row>'),20);self.assertEqual(second.count('<tr data-parameter-row>'),5)
        self.assertIn('id="parameter-value-20"',first)
        self.assertIn('data-searchable-select',first);self.assertNotIn('整批复制',first)

    def test_historical_batch_owns_its_task_and_current_picker_is_concrete(self):
        """历史参数、时间与来源任务固定，默认选择实际有效批次。"""
        c=self.case;c.batch(200,'parameters',product_id=1,task_id='b'*32)
        c.put('parameter_observations',id=500,batch_id=200,position=1,name='型号',value='20撕')
        old=self.client.get('/collections/products/123?batch=200').text
        current=self.client.get('/collections/products/123?module=parameters').text
        self.assertIn('历史批次',old);self.assertIn('20撕',old)
        self.assertIn('data-parameter-task href="/tasks/'+('b'*32),old)
        self.assertNotIn('id="parameter-value-20"',old)
        self.assertIn('value="20" selected',current);self.assertIn('最近有效资料',current)
        self.assertIn('data-parameter-source',current)

    def test_confirmed_empty_partial_and_read_error_are_distinct(self):
        """无参数、未保存明细和数据库读错均单独说明，读错不伪装成零项。"""
        c=self.case;c.batch(201,'parameters',product_id=1,status='no_parameters')
        empty=self.client.get('/collections/products/123?batch=201').text
        self.assertIn('本次未发现商品参数',empty);self.assertIn('共 0 项',empty)
        c.batch(202,'parameters',product_id=1,status='partial')
        partial=self.client.get('/collections/products/123?batch=202').text
        self.assertIn('部分完成',partial);self.assertIn('本批没有已保存的参数明细',partial)
        with patch('ecommerce.web.routes.pages.collection_query',side_effect=MySQLStorageError('模拟读错')):
            error=self.client.get('/collections/products/123?module=parameters').text
        self.assertIn('参数资料读取失败',error);self.assertIn('重新读取本批',error)
        self.assertNotIn('共 0 项',error);self.assertNotIn('本次未发现商品参数',error)
