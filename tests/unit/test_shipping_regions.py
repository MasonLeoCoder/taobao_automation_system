"""发货地区统计与明细筛选专项验证；所有样例使用临时库。"""
import csv
import io
import unittest
from datetime import timedelta

from ecommerce.modules.analysis.regions import parse_location
from ecommerce.modules.analysis.service import parse_filters
from tests.unit import test_search_analysis as fixtures


class ShippingRegionTests(unittest.TestCase):
    def setUp(self):
        """创建隔离样例；参数：无；返回：无。"""
        self.f=fixtures.SearchAnalysisTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_aliases_city_and_conservative_unknowns(self):
        """省份简称、全称及直辖市一致，城市单独出现不猜省份，复合地区不强行归属。"""
        examples={'浙江 金华':('浙江','金华'),'浙江省金华市婺城区':('浙江','金华'),
                  '上海':('上海','上海'),'上海市 浦东新区':('上海','上海'),
                  '广西壮族自治区 南宁市':('广西','南宁'),'内蒙古 呼和浩特':('内蒙古','呼和浩特'),
                  '浙江':('浙江','missing'),'浙江 多地发货':('浙江','unrecognized'),
                  '金华':('unrecognized',''),'海南藏族自治州':('unrecognized',''),
                  '青海 海南藏族自治州':('青海','海南藏族自治州'),'海外 美国':('unrecognized',''),
                  '浙江/广东':('unrecognized',''),'浙江 广东':('unrecognized',''),
                  None:('missing',''),'  ':('missing',''),'未知':('missing','')}
        for raw,expected in examples.items():
            with self.subTest(raw=raw):
                result=parse_location(raw)
                self.assertEqual((result['region'],result['city']),expected)

    def test_total_denominators_and_raw_text_preserved(self):
        """缺地区仍进总分母，城市按省内数统计，原文字段保持不变。"""
        locations=('浙江 金华','浙江省 金华市','浙江 嘉兴','上海',None,'海外 美国')
        for i,location in enumerate(locations,start=1):
            self.f.observation(i,location_text=location,price_value=None,sales_count=None)
        result=self.f.read(); distribution=result['shipping_distribution']
        self.assertEqual((distribution['total'],distribution['recorded'],distribution['missing'],distribution['unrecognized']),(6,5,1,1))
        self.assertEqual(sum(row['count'] for row in distribution['rows']),6)
        zhejiang=distribution['rows'][0]
        self.assertEqual((zhejiang['key'],zhejiang['count'],zhejiang['percent']),('浙江',3,50))
        self.assertEqual(zhejiang['cities'][0]['count'],2)
        self.assertAlmostEqual(zhejiang['cities'][0]['percent'],200/3)
        self.assertEqual([r['location_text'] for r in result['all_rows']],list(locations))
        self.assertEqual(self.f.read(region='missing')['pagination']['total'],1)
        self.assertEqual(self.f.read(region='unrecognized')['pagination']['total'],1)

    def test_latest_deduplication_ad_and_scope_are_shared(self):
        """先选最新商品观察再排广告，不从旧地区补位；原快照仍用原地区。"""
        self.f.observation(1,product=1,location_text='浙江 金华')
        self.f.batch(2,task_id=self.f.later_task,captured_at=self.f.now+timedelta(hours=1))
        self.f.observation(2,batch=2,product=1,location_text='广东 深圳',ad_status='identified')
        self.assertEqual(self.f.read()['shipping_distribution']['rows'][0]['key'],'广东')
        self.assertEqual(self.f.read(exclude_ads='1')['shipping_distribution']['total'],0)
        old=self.f.read(snapshot='task:'+self.f.task)
        self.assertEqual(old['shipping_distribution']['rows'][0]['key'],'浙江')

    def test_region_city_detail_export_and_other_summaries_unchanged(self):
        """地区只筛明细，缺价商品不遗漏；CSV及表单保持地区，图表范围不变。"""
        self.f.observation(1,location_text='浙江 金华',price_value=None)
        self.f.observation(2,location_text='浙江 嘉兴')
        self.f.observation(3,location_text='广东 深圳')
        original=self.f.read(); options={'region':'浙江','city':'金华'}
        selected=self.f.read(**options)
        self.assertEqual([r['id'] for r in selected['detail_rows']],[1])
        for name in ('stats','price_bins','scatter','price_composition','common_received','shipping_distribution'):
            self.assertEqual(selected[name],original[name],name)
        records=list(csv.reader(io.StringIO(self.f.client.get('/analysis/searches/export.csv',query_string=options).data.decode('utf-8-sig'))))
        self.assertEqual(len(records)-2,1)
        self.assertIn('发货地区筛选',records[0]);self.assertIn('浙江 / 金华',records[0])
        page=self.f.client.get('/analysis/searches',query_string=options).get_data(as_text=True)
        self.assertIn('name="region" value="浙江"',page)
        self.assertIn('name="city" value="金华"',page)
        self.assertIn('发货地区：浙江 / 金华',page)

    def test_pagination_empty_and_invalid_inputs(self):
        """地区统计不按明细分页截断，非法地区或无省份城市被拒绝，空样本无假比例。"""
        self.assertEqual(self.f.read()['shipping_distribution']['rows'],[])
        for i in range(1,24): self.f.observation(i,location_text='浙江 金华')
        result=self.f.read(region='浙江',city='金华',page='2')
        self.assertEqual(result['shipping_distribution']['total'],23)
        self.assertEqual(len(result['table_rows']),3)
        for values in ({'city':'金华'},{'region':'火星'},{'region':'missing','city':'金华'},
                       {'region':'浙江','city':'<script>'},{'region':'浙江','city':'金华\n'}):
            with self.subTest(values=values),self.assertRaises(ValueError): parse_filters(values)
