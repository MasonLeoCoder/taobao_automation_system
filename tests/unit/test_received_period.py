"""收货人数周期更正的范围、清单防护及页面/CSV一致性，样例只进入临时库。"""
import csv
import io
import unittest

import sqlalchemy as sa

from ecommerce.infrastructure.storage.list_sales_period import received_period_candidates,correct_received_periods
from tests.unit import test_search_analysis as fixtures


class ReceivedPeriodTests(unittest.TestCase):
    def setUp(self):
        """建立隔离数据；参数：无；返回：无。"""
        self.fixture=fixtures.SearchAnalysisTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tables=self.fixture.tables
        with self.fixture.engine.begin() as connection:
            connection.execute(self.tables['collection_batches'].update().values(platform='taobao'))

    def rows(self):
        """读取完整观察对照；参数：无；返回：按编号排序的字典列表。"""
        with self.fixture.engine.connect() as connection:
            return [dict(row) for row in connection.execute(sa.select(self.tables['list_observations']).order_by(self.tables['list_observations'].c.id)).mappings()]

    def test_only_verified_keyword_received_rows_are_corrected(self):
        """付款、明确月度、无法识别、其他平台/方式与暂存来源均不更正，仅修改候选周期。"""
        f=self.fixture
        f.observation(1,sales_period='unknown')
        f.observation(2,sales_text='500人付款',sales_count=500,sales_metric='paid_people',sales_precision='exact',sales_period='unknown')
        f.observation(3,sales_text='本月100人收货',sales_count=100,sales_precision='exact',sales_period='monthly')
        f.observation(4,sales_text='本月100人收货',sales_count=100,sales_precision='exact',sales_period='unknown')
        f.observation(5,sales_text='行业销量前10',sales_count=None,sales_precision='unknown',sales_parse_status='unrecognized',sales_period='unknown')
        for identifier,overrides in ((2,{'search_method':'image'}),(3,{'platform':'qianniu'}),(4,{'visibility':'staging'})):
            f.batch(identifier,platform='taobao' if identifier!=3 else 'qianniu',**{k:v for k,v in overrides.items() if k!='platform'})
            f.observation(4+identifier,batch=identifier,sales_period='unknown')
        before=self.rows()
        with f.engine.begin() as connection:
            expected=received_period_candidates(connection,self.tables)
            self.assertEqual([row['id'] for row in expected],[1])
            self.assertEqual(correct_received_periods(connection,self.tables,expected),[1])
            self.assertEqual(correct_received_periods(connection,self.tables,[]),[])
        after=self.rows()
        self.assertEqual(after,[{**row,'sales_period':'last_30_days'} if row['id']==1 else row for row in before])

    def test_manifest_change_is_rejected_before_any_update(self):
        """数量或来源在审核后变化时拒绝整份更正，原周期不被修改。"""
        self.fixture.observation(1,sales_period='unknown')
        with self.fixture.engine.begin() as connection:
            expected=received_period_candidates(connection,self.tables)
            connection.execute(self.tables['list_observations'].update().values(sales_count=20000))
            with self.assertRaises(ValueError): correct_received_periods(connection,self.tables,expected)
            self.assertEqual(connection.scalar(sa.select(self.tables['list_observations'].c.sales_period)),'unknown')

    def test_old_group_link_maps_to_actual_30_day_group_and_csv(self):
        """真实组已更正时旧链接、页面及CSV同步近30天，既有价格分段保持一致。"""
        self.fixture.observation(1)
        original=self.fixture.read()
        options={'group':'received_people:unknown','table_group':'received_people:unknown'}
        result=self.fixture.read(**options)
        self.assertEqual(result['group_label'],'收货人数 · 近30天')
        self.assertTrue(result['period_filter_updated'])
        self.assertEqual(result['filters']['table_group'],'received_people:last_30_days')
        self.assertEqual(result['price_bins'],original['price_bins'])
        query='?group=received_people:unknown&table_group=received_people:unknown'
        text=self.fixture.client.get('/analysis/searches'+query).get_data(as_text=True)
        self.assertIn('收货人数周期已更正为近30天',text)
        response=self.fixture.client.get('/analysis/searches/export.csv'+query)
        rows=list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertIn('收货人数 · 近30天',rows[0])
        self.assertEqual(rows[2][9],'收货人数 · 近30天')

    def test_real_unknown_group_and_explicit_monthly_are_preserved(self):
        """仍有真实未知周期或明确月度时不强制转换其筛选。"""
        self.fixture.observation(1)
        self.fixture.observation(2,sales_period='unknown')
        self.fixture.observation(3,sales_text='本月100人收货',sales_count=100,sales_period='monthly')
        result=self.fixture.read(group='received_people:unknown')
        self.assertFalse(result['period_filter_updated'])
        self.assertEqual(result['filters']['group'],'received_people:unknown')
        result=self.fixture.read(group='received_people:monthly')
        self.assertEqual(result['filters']['group'],'received_people:monthly')
