"""价位构成表与精确档位筛选，数据只进入临时库。"""
import csv
import io
import unittest

from ecommerce.modules.analysis.composition import parse_quantity_tier
from tests.unit import test_search_analysis as fixtures


class PriceCompositionTests(unittest.TestCase):
    def setUp(self):
        """创建独立分析样例；参数：无；返回：无。"""
        self.f=fixtures.SearchAnalysisTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def prepare(self):
        """准备不同精度、口径及缺价样例；参数：无；返回：分析结果。"""
        self.f.observation(1,price_value=3,sales_count=1000,sales_text='1000+人收货')
        self.f.observation(2,price_value=3,sales_count=1000,sales_text='1000人收货',sales_precision='exact')
        self.f.observation(3,price_value=4,sales_count=1000,sales_text='约1000人收货',sales_precision='approximate')
        self.f.observation(4,price_value=6)
        self.f.observation(5,price_value=None)
        self.f.observation(6,price_value=8,payload={'currency_symbol':'$'})
        self.f.observation(7,price_value=7,sales_count=500,sales_text='500人付款',sales_metric='paid_people',sales_precision='exact')
        self.f.observation(8,price_value=9,sales_count=None,sales_precision='unknown',sales_parse_status='not_provided')
        return self.f.read(group='received_people:last_30_days')

    def test_total_and_rows_keep_precision_groups_and_missing_prices(self):
        """表内合计与有效价位对齐，精确、加号、近似分别成档，其他口径单列无数据。"""
        r=self.prepare(); c=r['price_composition']
        self.assertEqual((c['total'],c['comparable'],c['unavailable'],c['missing_prices']),(6,4,2,2))
        self.assertEqual([t['label'] for t in c['tiers']],['1000人','1000+人','约1000人','1万+人'])
        self.assertEqual(sum(t['count'] for t in c['tiers']),4)
        self.assertEqual(sum(row['count'] for row in c['rows']),6)
        for row in c['rows']:
            self.assertEqual(sum(t['count'] for t in row['tiers'])+row['unavailable'],row['count'])
            for tier in row['tiers']:
                self.assertEqual(tier['tone'],next(t['tone'] for t in c['tiers'] if t['key']==tier['key']))

    def test_tier_drilldown_preserves_all_other_summary_and_export(self):
        """点击档位只筛选明细，原图表统计不变，CSV、清除与表单保留条件一致。"""
        original=self.prepare(); tier=next(t for t in original['price_composition']['tiers'] if t['label']=='1000+人')
        options={'bin':str(original['all_rows'][0]['price_bin']),'quantity_tier':tier['key'],'price_scale':original['filters']['price_scale']}
        r=self.f.read(**options)
        self.assertEqual([row['id'] for row in r['detail_rows']],[1])
        for name in ('stats','price_bins','scatter','price_cross','price_composition'):
            self.assertEqual(r[name],original[name],name)
        total=self.f.read(quantity_tier='received_people:last_30_days:lower_bound:10000')
        self.assertEqual([row['id'] for row in total['detail_rows']],[4])
        response=self.f.client.get('/analysis/searches/export.csv',query_string=options)
        records=list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertIn('展示档位筛选',records[0]); self.assertEqual(len(records)-2,1)
        self.assertEqual(records[2][0],'1')
        text=self.f.client.get('/analysis/searches',query_string=options).get_data(as_text=True)
        self.assertIn('name="quantity_tier"',text)
        self.assertIn('展示档位：收货人数',text)

    def test_actual_tiers_follow_low_and_high_values_and_keep_zero(self):
        """最高1000或10万时按实际值成档，零值单独保留，没有固定1万分层。"""
        for i,n in enumerate((0,100,1000,100000),start=1):
            self.f.observation(i,price_value=i,sales_count=n,sales_text=str(n)+'人收货',sales_precision='exact')
        r=self.f.read()
        self.assertEqual([t['label'] for t in r['price_composition']['tiers']],['0人','100人','1000人','10万人'])
        selected=self.f.read(quantity_tier='received_people:last_30_days:exact:0')
        self.assertEqual([row['id'] for row in selected['detail_rows']],[1])

    def test_invalid_tiers_rejected_and_changed_price_scope_clears_tier(self):
        """非法标识拒绝，价格段范围变更时同步清理旧档位，避免明细范围意外扩大。"""
        for raw in ('bad','received_people:weekly:exact:1','received_people:last_30_days:unknown:1',
                    'received_people:last_30_days:exact:-1','received_people:last_30_days:exact:01',
                    'received_people:last_30_days:exact:18446744073709551616'):
            with self.subTest(raw=raw),self.assertRaises(ValueError): parse_quantity_tier(raw)
        r=self.prepare()
        selected=self.f.read(bin='0',price_scale='a'*16,quantity_tier='received_people:last_30_days:lower_bound:1000')
        self.assertIsNone(selected['filters']['bin']); self.assertEqual(selected['filters']['quantity_tier'],'')

    def test_empty_sample_and_unavailable_group_are_honest(self):
        """空样本不造档位，无当前口径的数据明确计入未提供数量，不补零。"""
        self.assertEqual(self.f.read()['price_composition']['total'],0)
        self.f.observation(1)
        c=self.f.read(group='received_people:monthly')['price_composition']
        self.assertEqual(c['tiers'],[])
        self.assertEqual((c['total'],c['unavailable']),(1,1))
