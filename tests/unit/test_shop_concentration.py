"""店铺集中度的分母、合计守恒、分组身份、选样和CSV关联专项测试。"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import csv
import io
import unittest

from ecommerce.modules.analysis.concentration import competition_concentration
from ecommerce.modules.analysis.shop_concentration import shop_concentration
from tests.unit.test_concentration import sample_row
from tests.unit import test_search_analysis as fixtures


def shop_row(identifier, count, key):
    """构造纯内存店铺行；参数：身份、人数和分组键；返回：分析行。"""
    return {**sample_row(identifier,count),'shop_key':key,'shop_name':key,
            'shop_basis':'按店铺名称近似分组' if key and key.startswith('name:') else '店铺公开路由'}


class ShopConcentrationCalculationTests(unittest.TestCase):
    def test_denominator_coverage_cr_hhi_and_children_conservation(self):
        """缺店铺单列，不混入分母；父子数量、合计和占比一致，原集中度不变。"""
        rows=[shop_row(1,100,'name:A'),shop_row(2,60,'shop1.taobao.com'),shop_row(3,40,'name:A'),
              shop_row(4,20,'shop1.taobao.com'),shop_row(5,10,'name:C'),shop_row(6,5,None)]
        c=competition_concentration(rows); original=deepcopy(c)
        s=shop_concentration(c)
        self.assertEqual((s['known_count'],s['missing_count'],s['total'],s['missing_total']),(5,1,230,5))
        self.assertEqual(s['product_coverage'],Decimal(5)*100/6)
        self.assertEqual(s['quantity_coverage'],Decimal(230)*100/235)
        self.assertEqual((s['reliable_count'],s['name_count']),(1,2))
        self.assertEqual(s['metrics'][0]['value'],100)
        self.assertIsNone(s['metrics'][1]['value'])
        self.assertEqual(s['hhi'],Decimal(140**2+80**2+10**2)*10000/Decimal(230)**2)
        self.assertEqual(s['groups'][-1]['cumulative'],100)
        for group in s['groups']:
            self.assertEqual(len(group['members']),group['count'])
            self.assertEqual(sum(r['sales_count'] for r in group['members']),group['total'])
            self.assertAlmostEqual(float(sum(r['shop_share'] for r in group['members'])),100)
            self.assertEqual(group['head_count']+group['middle_count']+group['tail_count'],group['count'])
            self.assertEqual([r['rank'] for r in group['members']],sorted(r['rank'] for r in group['members']))
        self.assertEqual(c,original)

    def test_equal_shop_totals_use_first_product_rank_and_keep_distinct_routes(self):
        """店铺同值按最前商品序号；同名不同路由不擅自合并。"""
        rows=[shop_row(1,60,'route:B'),shop_row(2,60,'route:A'),shop_row(3,40,'route:A'),shop_row(4,40,'route:B')]
        for row in rows: row['shop_name']='同名店'
        s=shop_concentration(competition_concentration(list(reversed(rows))))
        self.assertEqual([g['key'] for g in s['groups']],['route:B','route:A'])
        self.assertEqual([g['mean'] for g in s['groups']],[50,50])
        self.assertEqual([r['id'] for r in s['groups'][0]['members']],[1,4])

    def test_only_same_top50_and_segment_counts(self):
        """不从前50之外补店铺，分段按全局序号，合计相同按最前序号排。"""
        rows=[shop_row(i,1000,'name:A' if i<=10 else 'name:B' if i<=30 else 'name:C') for i in range(1,52)]
        rows[-1]['shop_key']=None
        s=shop_concentration(competition_concentration(rows))
        self.assertEqual((s['selected_count'],s['known_count'],s['missing_count']),(50,50,0))
        self.assertEqual([g['key'] for g in s['groups']],['name:B','name:C','name:A'])
        self.assertEqual(s['groups'][0]['middle_count'],20)
        self.assertEqual(s['groups'][1]['tail_count'],20)
        self.assertEqual(s['groups'][2]['head_count'],10)

    def test_empty_missing_and_zero_do_not_create_percentages(self):
        """无组或全零不产生比值，未知店铺不合并，单组均匀HHI参照正确。"""
        for rows in ([],[shop_row(1,100,None)],[shop_row(i,0,'name:'+str(i)) for i in range(1,5)]):
            s=shop_concentration(competition_concentration(rows))
            self.assertIsNone(s['hhi']);self.assertIsNone(s['uniform_hhi'])
            self.assertTrue(all(m['value'] is None for m in s['metrics']))
            self.assertTrue(all(g['share'] is None for g in s['groups']))
            self.assertTrue(all(r['shop_share'] is None for g in s['groups'] for r in g['members']))
        s=shop_concentration(competition_concentration([shop_row(1,100,'name:A')]))
        self.assertEqual((s['hhi'],s['uniform_hhi']),(10000,10000))


class ShopConcentrationScopeTests(unittest.TestCase):
    def setUp(self):
        """建立隔离样例；参数：无；返回：无。"""
        self.f=fixtures.SearchAnalysisTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_drilldown_export_keys_and_existing_analysis_unchanged(self):
        """汇总与商品CSV按分组键对应；顶店铺名单查询不改变原统计或其他导出列。"""
        for i in range(1,61):
            name='=测试店' if i>=58 else '店铺'+str(i%12)
            self.f.observation(i,sales_count=i,shop_name=name,price_value=None if i%2 else i)
        original=self.f.read()
        selected=self.f.read(finding='shop_cr3',sort='concentration')
        expected={r['id'] for g in original['shop_concentration']['groups'][:3] for r in g['members']}
        self.assertEqual({r['id'] for r in selected['detail_rows']},expected)
        for name in ('stats','concentration','middle','price_bins','shipping_distribution','shop_concentration'):
            self.assertEqual(original[name],selected[name],name)
        queried=self.f.read(finding='shop_all',q='不存在的标题')
        self.assertEqual(queried['detail_rows'],[])
        self.assertEqual(queried['shop_concentration'],original['shop_concentration'])
        summary=list(csv.reader(io.StringIO(self.f.client.get('/analysis/searches/shop-summary.csv').data.decode('utf-8-sig'))))
        items=list(csv.reader(io.StringIO(self.f.client.get('/analysis/searches/export.csv?finding=shop_all&sort=concentration').data.decode('utf-8-sig'))))
        self.assertEqual(len(items)-2,50)
        self.assertEqual(len(summary)-2,original['shop_concentration']['shop_count'])
        self.assertIn('店铺计算分母',items[0])
        self.assertIn('店铺分组键',items[1])
        for group in summary[2:]:
            rows=[r for r in items[2:] if r[21]==group[1]]
            self.assertEqual(len(rows),int(group[4]))
            self.assertEqual(sum(int(r[8]) for r in rows),int(group[5]))
        self.assertTrue(any(r[2].startswith("'=测试店") for r in summary[2:]))
        old=list(csv.reader(io.StringIO(self.f.client.get('/analysis/searches/export.csv').data.decode('utf-8-sig'))))
        self.assertEqual(len(old[1]),21)

    def test_latest_observation_before_shop_missing_and_ad_filter(self):
        """最新记录缺店铺不补旧值，广告先筛，单次范围仍独立。"""
        self.f.observation(1,product=1,shop_name='旧店',sales_count=1000)
        self.f.observation(2,product=2,shop_name='广告店',sales_count=2000)
        self.f.batch(2,task_id=self.f.later_task,captured_at=self.f.now+timedelta(hours=1))
        self.f.observation(3,batch=2,product=1,shop_name=None,sales_count=1000)
        self.f.observation(4,batch=2,product=2,shop_name='广告店',sales_count=2000,ad_status='identified')
        r=self.f.read(exclude_ads='1')
        self.assertEqual((r['shop_concentration']['known_count'],r['shop_concentration']['missing_count']),(0,1))
        self.assertEqual(self.f.read(snapshot='task:'+self.f.task)['shop_concentration']['shop_count'],2)
