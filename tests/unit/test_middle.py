"""腰部固定区间、统计分母、店铺重合及明细导出的隔离验证。"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import csv
import io
import unittest

from ecommerce.modules.analysis.concentration import competition_concentration
from ecommerce.modules.analysis.middle import middle_performance
from tests.unit.test_concentration import sample_row
from tests.unit import test_search_analysis as fixtures


def middle_row(identifier, count, **values):
    """生成腰部内存样例；参数：身份、人数及覆盖字段；返回：含价格/店铺的分析行。"""
    return sample_row(identifier,count,price=Decimal(identifier),price_usable=True,
                      shop_key='s'+str(identifier),shop_name='店铺'+str(identifier),shop_basis='名称近似',**values)


class MiddleCalculationTests(unittest.TestCase):
    def test_fixed_slice_means_price_quartiles_coverage_and_immutability(self):
        """只取同一名单11～30件；缺价不剔商品，头腰均值和价格分母各自独立。"""
        rows=[middle_row(i,61-i) for i in range(1,61)]
        for row in rows:
            if row['id'] in (11,12): row.update(price=None,price_usable=False)
        c=competition_concentration(rows); original=deepcopy(c)
        m=middle_performance(c)
        self.assertEqual([r['id'] for r in m['members']],list(range(11,31)))
        self.assertEqual(m['head']['count'],10)
        self.assertEqual(m['head']['quantity_mean'],Decimal('55.5'))
        self.assertEqual(m['summary']['quantity_mean'],Decimal('40.5'))
        self.assertEqual(m['summary']['quantity_median'],Decimal('40.5'))
        self.assertEqual(m['summary']['price_count'],18)
        self.assertEqual(m['summary']['price_median'],Decimal('21.5'))
        self.assertEqual((m['summary']['price_q1'],m['summary']['price_q3']),(Decimal('17.25'),Decimal('25.75')))
        self.assertEqual(m['quantity_share'],Decimal(810)*100/1775)
        self.assertEqual(c,original)

    def test_shop_overlap_uses_known_waist_shops_not_products(self):
        """重合比例分母为腰部已知店铺数，缺失单列，同店多个链接不重复计店铺。"""
        rows=[middle_row(i,61-i) for i in range(1,31)]
        for row in rows:
            i=row['id']
            if i<=12: row.update(shop_key='shared',shop_name='头部同店')
            if i in (13,14): row.update(shop_key='waist-only',shop_name='腰部同店')
            if i==15: row.update(shop_key=None,shop_name=None)
        m=middle_performance(competition_concentration(rows))
        self.assertEqual((m['summary']['shop_count'],m['summary']['missing_shops']),(17,1))
        self.assertEqual((m['multi_shop_count'],m['overlap_shop_count'],m['overlap_product_count']),(2,1,2))
        self.assertEqual(m['overlap_shop_percent'],Decimal(100)/17)
        self.assertEqual(next(s for s in m['shops'] if s['key']=='shared')['head_count'],10)

    def test_partial_empty_zero_and_missing_timestamps(self):
        """不足11件没有腰部，不足30按实际区间；全零不制造占比，无时间明确缺失。"""
        for size in (0,10,11,20):
            with self.subTest(size=size):
                rows=[middle_row(i,0,captured_at=None) for i in range(1,size+1)]
                m=middle_performance(competition_concentration(rows))
                self.assertEqual(m['summary']['count'],max(0,size-10))
                self.assertIsNone(m['quantity_share'])
                self.assertIsNone(m['earliest'])
                self.assertEqual(m['missing_times'],max(0,size-10))
                if size<=10: self.assertIsNone(m['summary']['quantity_median'])

    def test_tier_precision_both_edges_and_time_span(self):
        """两侧边界用全部有效同值数，档位保留精度，时间按腰部实际所选观察计算。"""
        rows=[middle_row(i,2000,sales_precision='exact' if i%2 else 'lower_bound',
                         captured_at=datetime(2026,10,28,12)+timedelta(minutes=i)) for i in range(1,61)]
        m=middle_performance(competition_concentration(rows))
        self.assertEqual([(b['edge'],b['tied'],b['included']) for b in m['boundaries']],[(10,60,20),(30,60,20)])
        self.assertEqual([(t['label'],t['count']) for t in m['tiers']],[('2000人',10),('2000+人',10)])
        self.assertEqual((m['earliest'].minute,m['latest'].minute),(11,30))


class MiddleScopeTests(unittest.TestCase):
    def setUp(self):
        """建立隔离库；参数：无；返回：无。"""
        self.f=fixtures.SearchAnalysisTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_middle_tier_missing_price_query_export_and_unchanged_summaries(self):
        """腰部及档位只筛明细，缺价仍保留；导出带头腰口径及覆盖数，原统计不变。"""
        for i in range(1,61): self.f.observation(i,sales_count=i,price_value=None if i==50 else i)
        original=self.f.read(); options={'finding':'middle','sort':'concentration'}
        selected=self.f.read(**options)
        self.assertEqual([r['id'] for r in selected['detail_rows']],list(range(50,30,-1)))
        for name in ('stats','concentration','middle','shipping_distribution','price_bins','common_received'):
            self.assertEqual(original[name],selected[name],name)
        tier=self.f.read(**options,quantity_tier='received_people:last_30_days:lower_bound:50')
        self.assertEqual([r['id'] for r in tier['detail_rows']],[50])
        self.assertEqual(self.f.read(**options,q='不存在的商品')['middle'],original['middle'])
        response=self.f.client.get('/analysis/searches/export.csv',query_string=options)
        records=list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(len(records)-2,20)
        self.assertIn('头部件数',records[0]);self.assertIn('腰部有效价格件数',records[0])
        self.assertIn('重合比例分母（腰部已知店铺数）',records[0])
        self.assertEqual((records[2][0],records[-1][0]),('50','31'))

    def test_boundary_all_candidates_shared_shops_and_latest_ad_scope(self):
        """边界可看名单外同值商品但不扩大腰部；重合店铺仅选腰部，旧人数不补位。"""
        for i in range(1,61): self.f.observation(i,sales_count=2000,price_value=None)
        original=self.f.read()
        for finding in ('middle_boundary10','middle_boundary30'):
            r=self.f.read(finding=finding,sort='concentration',page='3')
            self.assertEqual(len(r['detail_rows']),60)
            self.assertEqual(len(r['table_rows']),20)
            self.assertEqual(r['middle'],original['middle'])
        self.assertEqual(len(self.f.read(finding='middle_shared')['detail_rows']),20)
        self.f.batch(2,task_id=self.f.later_task,captured_at=self.f.now+timedelta(hours=1))
        self.f.observation(61,product=1,batch=2,sales_count=None,sales_parse_status='not_provided')
        self.f.observation(62,product=2,batch=2,ad_status='identified')
        r=self.f.read(exclude_ads='1')
        self.assertEqual(r['concentration']['eligible_count'],58)
        self.assertEqual(r['middle']['members'][0]['id'],13)
        old=self.f.read(snapshot='task:'+self.f.task)
        self.assertEqual(old['middle']['members'][0]['id'],11)
