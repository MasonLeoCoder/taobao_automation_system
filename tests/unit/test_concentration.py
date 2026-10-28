"""前50件集中度的数学口径、稳定取样、观察范围及导出验证。"""
from datetime import datetime, timedelta
from decimal import Decimal
import csv
import io
import unittest

from ecommerce.modules.analysis.concentration import competition_concentration
from tests.unit import test_search_analysis as fixtures


def sample_row(identifier, count, **values):
    """构造内存计算样例；参数：身份、人数及覆盖值；返回：分析行，不写数据库。"""
    return {'id':identifier,'product_id':identifier,'platform_product_id':str(identifier),
            'quantity_usable':True,'sales_group':'received_people:last_30_days',
            'sales_precision':'lower_bound','sales_count':count,'page_number':1,
            'slot_index':identifier,'captured_at':datetime(2026,10,28,12),**values}


class ConcentrationCalculationTests(unittest.TestCase):
    def test_top50_denominator_cr_and_hhi_use_only_selected_values(self):
        """排除第51件后的数值只用前50合计，不受尾部商品或价格影响。"""
        values=[100,50]+[10]*48+[1]*10
        c=competition_concentration([sample_row(i,n) for i,n in enumerate(values,start=1)])
        self.assertEqual((c['eligible_count'],c['count'],c['total']),(60,50,630))
        self.assertEqual(c['metrics'][0]['numerator'],160)
        self.assertEqual(c['metrics'][0]['value'],Decimal(160)*100/630)
        self.assertEqual(c['metrics'][1]['numerator'],230)
        self.assertEqual(c['hhi'],Decimal(10000+2500+48*100)*10000/Decimal(630)**2)
        self.assertEqual(c['uniform_hhi'],200)
        self.assertEqual(c['members'][-1]['cumulative'],100)
        self.assertIsNone(c['boundary'])

    def test_boundary_ties_follow_page_slot_new_time_and_id(self):
        """人数优先，同值按页码和位置，再按较新时间及稳定观察ID选取。"""
        rows=[sample_row(i,5000) for i in range(1,50)]
        old=datetime(2026,10,27,12); new=datetime(2026,10,28,12)
        rows += [sample_row(100,2000,page_number=2,slot_index=1),
                 sample_row(101,2000,page_number=1,slot_index=2),
                 sample_row(102,2000,page_number=1,slot_index=1,captured_at=old),
                 sample_row(104,2000,page_number=1,slot_index=1,captured_at=new),
                 sample_row(103,2000,page_number=1,slot_index=1,captured_at=new),
                 sample_row(105,2000,page_number=None,slot_index=None)]
        c=competition_concentration(list(reversed(rows)))
        self.assertEqual(c['members'][-1]['id'],103)
        self.assertEqual(c['boundary'],{'value':2000,'tied':6,'included':1,'excluded':5})
        self.assertEqual(c['members'][-1]['tie_count'],6)

    def test_invalid_groups_precision_and_identity_are_excluded_before_selecting(self):
        """未知身份、无数量、不同周期及近似值互斥计数；有效零值保留。"""
        rows=[sample_row(1,0),sample_row(2,1000,sales_precision='exact'),
              sample_row(3,100000,product_id=None),sample_row(4,None,quantity_usable=False),
              sample_row(5,100000,sales_group='received_people:monthly'),
              sample_row(6,100000,sales_precision='approximate')]
        c=competition_concentration(rows)
        self.assertEqual((c['count'],c['total'],c['excluded'],c['zero_count']),(2,1000,4,1))
        self.assertEqual(c['reasons'],dict(identity=1,missing=1,group=1,approximate=1))
        self.assertEqual([r['id'] for r in c['members']],[2,1])
        self.assertEqual(c['hhi'],10000)
        self.assertTrue(all(m['value'] is None for m in c['metrics']))

    def test_empty_zero_and_uniform_samples_do_not_invent_ratios(self):
        """空样本与全零无占比，均匀50件HHI为200且CR分母一致。"""
        for rows in ([],[sample_row(i,0) for i in range(1,12)]):
            c=competition_concentration(rows)
            self.assertIsNone(c['hhi']);self.assertIsNone(c['uniform_hhi'])
            self.assertTrue(all(m['value'] is None for m in c['metrics']))
            self.assertTrue(all(r['share'] is None for r in c['members']))
        c=competition_concentration([sample_row(i,1000) for i in range(1,51)])
        self.assertEqual(c['hhi'],200)
        self.assertEqual([m['value'] for m in c['metrics']],[6,20])


class ConcentrationScopeTests(unittest.TestCase):
    def setUp(self):
        """初始化隔离数据库；参数：无；返回：无。"""
        self.f=fixtures.SearchAnalysisTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_latest_observation_selected_before_validity_and_ads_no_old_fallback(self):
        """历史汇总不补旧人数，单次范围可独立分析，先去重再筛选广告。"""
        self.f.observation(1,product=1,sales_count=5000)
        self.f.observation(2,product=2,sales_count=4000)
        self.f.batch(2,task_id=self.f.later_task,captured_at=self.f.now+timedelta(hours=1))
        self.f.observation(3,batch=2,product=1,sales_count=None,sales_parse_status='not_provided')
        self.f.observation(4,batch=2,product=2,sales_count=10000,ad_status='identified')
        c=self.f.read(exclude_ads='1')['concentration']
        self.assertEqual((c['source_count'],c['count'],c['reasons']['missing']),(1,0,1))
        old=self.f.read(snapshot='task:'+self.f.task)['concentration']
        self.assertEqual((old['count'],old['total']),(2,9000))

    def test_drilldown_pagination_export_and_other_summaries(self):
        """前50名单支持查询分页和CSV；所有原图表与集中度分母不随明细变化。"""
        for i in range(1,61): self.f.observation(i,sales_count=i,price_value=None if i%2 else i)
        original=self.f.read(); options={'finding':'top50','sort':'concentration'}
        selected=self.f.read(**options)
        self.assertEqual([r['id'] for r in selected['detail_rows']],list(range(60,10,-1)))
        self.assertEqual(selected['concentration']['total'],1775)
        page2=self.f.read(**options,page='2')
        self.assertEqual(page2['table_rows'][0]['id'],40)
        for name in ('stats','price_bins','price_composition','common_received','shipping_distribution','concentration'):
            self.assertEqual(selected[name],original[name],name)
        queried=self.f.read(**options,q='没有这个商品')
        self.assertEqual(queried['detail_rows'],[])
        self.assertEqual(queried['concentration'],original['concentration'])
        for finding,size in (('top3',3),('top10',10),('top50',50)):
            response=self.f.client.get('/analysis/searches/export.csv',query_string={'finding':finding,'sort':'concentration'})
            records=list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
            self.assertEqual(len(records)-2,size)
            self.assertIn('集中度参与件数',records[0]);self.assertIn('1775',records[0])
            self.assertEqual(records[2][0],'60')
        html=self.f.client.get('/analysis/searches',query_string=options).get_data(as_text=True)
        self.assertIn('name="finding" value="top50"',html)
        self.assertIn('按集中度计算顺序',html)
        self.assertEqual(self.f.read(sort='concentration')['filters']['sort'],'position')
