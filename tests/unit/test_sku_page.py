"""SKU页整批统计、价格排序、状态筛选及原文字段的只读验证。"""
from datetime import datetime
from decimal import Decimal
import copy
import unittest

from ecommerce.modules.collection.queries import query
from ecommerce.modules.collection.sku_view import present_record
from tests.unit import test_sku_list_images as fixtures


class SkuPageTests(unittest.TestCase):
    def setUp(self):
        """创建两批独立规格；参数：无；返回：无。"""
        self.host=fixtures.SkuListImageTests();self.host.setUp();self.addCleanup(self.host.doCleanups)
        self.case=self.host.fixture;self.settings=self.host.settings;self.client=self.host.client
        c=self.case
        c.batch(60,'skus',product_id=1,task_id=c.task,captured_at=datetime(2026,10,24))
        c.put('sku_observations',id=100,batch_id=60,position=1,sku_name='旧批价格',price_text='¥99',price_value=99,
              price_status='collected',stock_status='out_of_stock',currency='CNY',payload={'stock_evidence':'旧批明确售罄'})

    def sku(self,number,**values):
        """加入合成规格；参数：顺序及覆盖字段；返回：规格编号。"""
        return self.host.sku(number,**{'price_text':f'¥{number}','price_status':'collected','currency':'CNY',
            'stock_status':'selectable','combination_confirmed':True,'captured_at':self.case.now,**values})

    def records(self,**options):
        """读取专用规格视图；参数：筛选排序分页；返回：查询结果。"""
        return query(self.settings,'records',batch_id=70,with_sku_overview=True,page=options.pop('page',1),**options)

    def test_counts_cover_full_batch_while_filters_and_pagination_only_limit_rows(self):
        """概览不受当前分页/状态限制，历史批不混入；参数：无；返回：断言。"""
        for number in range(1,46):self.sku(number)
        self.sku(46,stock_status='out_of_stock',price_status='out_of_stock',price_value=None)
        self.sku(47,stock_status='unavailable_unconfirmed',price_status='unavailable_unconfirmed',price_value=None)
        self.sku(48,stock_status=None,price_status='not_read',price_value=None)
        self.sku(49,stock_status='SELECTABLE',price_status='not_read',price_value=None)
        data=self.records(page=2,stock_filter='selectable',sort_by='price',order='desc')
        self.assertEqual((data['total'],len(data['records'])),(45,20))
        self.assertEqual(data['records'][0]['position'],25)
        overview=data['sku_overview'];self.assertEqual(overview['total'],49)
        self.assertEqual(overview['counts'],{'selectable':45,'out_of_stock':1,'unavailable_unconfirmed':1,'unknown':2})
        self.assertEqual((overview['ranges'][0]['min'],overview['ranges'][0]['max']),('1.000000','45.000000'))
        self.assertEqual([row['position'] for row in self.records(stock_filter='unknown')['records']],[48,49])
        self.assertEqual(self.records(stock_filter='out_of_stock')['records'][0]['position'],46)
        self.assertEqual(self.records(stock_filter='unavailable_unconfirmed')['total'],1)

    def test_prices_numeric_zero_valid_unknown_last_and_currency_grouped(self):
        """只用已确认金额，未确认不当作零，币种独立排序；参数：无；返回：断言。"""
        for number,amount in [(1,10),(2,2),(3,0)]:self.sku(number,price_value=amount)
        self.sku(4,price_value=1,price_status='not_read')
        self.sku(5,price_value=1,currency=None)
        self.sku(6,price_value=1,currency='USD')
        self.sku(7,price_value=None)
        ascending=self.records(sort_by='price',order='asc');descending=self.records(sort_by='price',order='desc')
        self.assertEqual([row['position'] for row in ascending['records'][:4]],[3,2,1,6])
        self.assertEqual([row['position'] for row in descending['records'][:4]],[1,2,3,6])
        self.assertEqual({row['position'] for row in descending['records'][4:]},{4,5,7})
        summary=ascending['sku_overview'];self.assertEqual((summary['priced'],summary['unpriced']),(4,3))
        self.assertEqual([item['currency'] for item in summary['ranges']],['CNY','USD'])
        page=self.client.get('/collections/products/123?batch=70&sort=price&order=asc').text
        self.assertIn('¥0.00～¥10.00',page);self.assertIn('不同币种分别统计',page)

    def test_batch_binding_and_pending_form_reset_and_parameters(self):
        """批次归属固定，切批重置状态分页，排序链接保留本批和状态；参数：无；返回：断言。"""
        self.sku(1);self.host.image(1,201)
        page=self.client.get('/collections/products/123?module=skus&kind=media&stock=selectable').text
        self.assertIn('data-sku-page',page);self.assertIn('当前批次 70',page)
        self.assertIn('name="batch"',page);self.assertIn('/collections/files/301',page)
        self.assertIn('stock=selectable',page);self.assertNotIn('media_page=',page)
        old=self.client.get('/collections/products/123?batch=60').text
        self.assertIn('旧批价格',old);self.assertIn('¥99.00',old);self.assertNotIn('/collections/files/301',old)
        self.assertEqual(self.client.get('/collections/products/456?batch=70').status_code,404)
        self.assertEqual(self.client.get('/collections/products/123?batch=70&stock=bogus').status_code,400)

    def test_raw_states_evidence_and_replacement_characters_retained(self):
        """规格名、原属性和依据只读显示，不猜乱码或重判库存；参数：无；返回：断言。"""
        attributes=[{'group':'颜色','name':'粉色','value_id':'a'},{'group':'套餐','name':'2卷','value_id':'b'}]
        self.sku(1,sku_name='替换��纸',properties=attributes,stock_status='out_of_stock',price_status='out_of_stock',price_value=None,
                 payload={'stock_evidence':'页面规格禁用：disabled属性','unavailability_evidence':['disabled属性'],'price_reason':'按原规则缺货'})
        before=query(self.settings,'records',batch_id=70,kind='skus')['records']
        page=self.client.get('/collections/products/123?batch=70').text
        self.assertIn('规格文字异常',page);self.assertIn('替换��纸',page);self.assertIn('页面规格禁用：disabled属性',page)
        self.assertIn('颜色',page);self.assertIn('粉色',page);self.assertNotIn("{'group':",page)
        self.assertIn('复制规格名称',page);self.assertIn('查看规格详情',page)
        self.assertEqual(before,query(self.settings,'records',batch_id=70,kind='skus')['records'])

    def test_unknown_price_empty_filter_and_scope_errors(self):
        """空筛选不伪造无SKU，非法范围不放行，默认查询不扩展返回；参数：无；返回：断言。"""
        self.sku(1,price_value=None,price_status='not_read',stock_status=None)
        page=self.client.get('/collections/products/123?batch=70&stock=selectable').text
        self.assertIn('共 1 种规格',page);self.assertIn('没有符合当前状态的规格',page)
        self.assertIn('暂无可确认的价格区间',page)
        generic=query(self.settings,'records',batch_id=70)
        self.assertNotIn('sku_overview',generic);self.assertNotIn('batch_note',generic)
        with self.assertRaises(ValueError):query(self.settings,'records',batch_id=20,with_sku_overview=True)
        with self.assertRaises(ValueError):self.records(stock_filter='')
        with self.assertRaises(ValueError):query(self.settings,'records',batch_id=70,stock_filter='all')

    def test_view_normalization_does_not_mutate_source_or_invent_attributes(self):
        """异常属性不转成伪规格，原文字、名称及币种保持；参数：无；返回：断言。"""
        raw={'id':1,'sku_name':'原名称<script>','properties':[{'group':'规格','name':'原名称<script>'}],
             'price_status':'collected','price_value':Decimal('3.35'),'currency':'CNY','stock_status':'selectable',
             'payload':{'stock_evidence':'原依据'},'combination_confirmed':True}
        before=copy.deepcopy(raw);view=present_record(raw)
        self.assertEqual(raw,before);self.assertEqual(view['brief_attributes'],[]);self.assertEqual(view['display_price'],'¥3.35')
        self.assertFalse(present_record({**raw,'properties':'旧格式'})['properties_readable'])
        self.assertEqual(present_record({**raw,'currency':None})['display_price'],'未确认')
