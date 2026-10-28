"""最常见收货档位卡片专项验证；仅使用隔离样例。"""
import csv
import io
import unittest

from ecommerce.modules.analysis.service import parse_filters
from tests.unit import test_search_analysis as fixtures


class CommonReceivedTests(unittest.TestCase):
    def setUp(self):
        """初始化隔离数据；参数：无；返回：无。"""
        self.f=fixtures.SearchAnalysisTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_mode_is_not_maximum_and_follows_data_scale(self):
        """最多商品所在档位可低于最高人数，不使用固定一万门槛。"""
        for i,n in enumerate((500,500,1000),start=1):
            self.f.observation(i,sales_count=n)
        common=self.f.read()['common_received']
        self.assertEqual(common['total'],3)
        self.assertEqual([(t['label'],t['count']) for t in common['tiers']],[('500+人',2)])
        self.assertAlmostEqual(common['tiers'][0]['percent'],200/3)
        self.f.observation(4,sales_count=100000)
        self.f.observation(5,sales_count=100000)
        common=self.f.read()['common_received']
        self.assertEqual([t['label'] for t in common['tiers']],['500+人','10万+人'])
        self.assertEqual([t['percent'] for t in common['tiers']],[40,40])

    def test_precision_zero_and_all_ties_are_preserved(self):
        """零值、精确、下界和近似分别统计，全部并列而非任意取一个。"""
        for i,precision in enumerate(('exact','lower_bound','approximate'),start=1):
            self.f.observation(i,sales_count=0,sales_precision=precision)
        common=self.f.read()['common_received']
        self.assertEqual([t['label'] for t in common['tiers']],['0人','0+人','约0人'])
        self.assertTrue(all(t['count']==1 for t in common['tiers']))

    def test_denominator_ad_scope_period_and_price_independence(self):
        """仅近30天有效收货人数作分母，广告按显式筛选，价格缺失仍纳入。"""
        self.f.observation(1,sales_count=1000,price_value=None)
        self.f.observation(2,sales_count=1000,ad_status='identified')
        self.f.observation(3,sales_count=500)
        self.f.observation(4,sales_count=500,sales_metric='paid_people')
        self.f.observation(5,sales_count=500,sales_period='monthly')
        self.f.observation(6,sales_count=None,sales_parse_status='not_provided')
        r=self.f.read(group='paid_people:last_30_days')
        self.assertEqual(r['common_received']['total'],3)
        self.assertEqual(r['common_received']['tiers'][0]['label'],'1000+人')
        self.assertEqual(self.f.read(exclude_ads='1')['common_received']['total'],2)
        self.assertEqual(len(self.f.read(exclude_ads='1')['common_received']['tiers']),2)

    def test_click_export_includes_missing_price_and_keeps_other_summaries(self):
        """卡片档位含缺价商品，构成表旧链接仍排除缺价，明细与CSV数目一致。"""
        self.f.observation(1,sales_count=1000,price_value=None)
        self.f.observation(2,sales_count=1000)
        self.f.observation(3,sales_count=500)
        token='received_people:last_30_days:lower_bound:1000'
        base=self.f.read()
        selected=self.f.read(finding='quantity',quantity_tier=token)
        self.assertEqual([r['id'] for r in selected['detail_rows']],[1,2])
        self.assertEqual([r['id'] for r in self.f.read(quantity_tier=token)['detail_rows']],[2])
        for name in ('stats','price_bins','scatter','price_composition','common_received'):
            self.assertEqual(base[name],selected[name],name)
        response=self.f.client.get('/analysis/searches/export.csv',query_string={'finding':'quantity','quantity_tier':token})
        records=list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(len(records)-2,2)
        self.assertIn('展示档位筛选',records[0])
        stale=self.f.read(bin='0',price_scale='a'*16,finding='quantity',quantity_tier=token)
        self.assertEqual(stale['filters']['quantity_tier'],'')
        self.assertEqual(stale['filters']['finding'],'')
        with self.assertRaises(ValueError): parse_filters({'finding':'quantity'})

    def test_empty_state_and_legacy_link(self):
        """无数据不造档位，旧一万链接兼容，卡片不再展示固定门槛。"""
        self.assertEqual(self.f.read()['common_received'],{'total':0,'tiers':[]})
        self.f.observation(1,sales_count=10000,sales_metric='paid_people')
        self.assertEqual(self.f.read()['common_received']['tiers'],[])
        page=self.f.client.get('/analysis/searches').get_data(as_text=True)
        card=page.split('id="sa-common-quantity"',1)[1].split('</article>',1)[0]
        self.assertIn('暂无可分析的收货人数',card)
        self.assertNotIn('达到1万',card)
        self.assertEqual(len(self.f.read(finding='high')['detail_rows']),1)
