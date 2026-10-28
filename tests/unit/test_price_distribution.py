"""自动价格分段的覆盖、互斥、极端价格和业务联动验证，样例不进入正式库。"""
import csv
from decimal import Decimal
import io
import random
import unittest

from ecommerce.modules.analysis.prices import contains_price, price_distribution
from tests.unit import test_search_analysis as fixtures


class PriceDistributionTests(unittest.TestCase):
    def check_partition(self, prices):
        """核对真实数值各有唯一归属；参数：价格；返回：生成结果。"""
        values = [Decimal(str(value)) for value in prices]
        result = price_distribution(values)
        bins = result['bins']
        self.assertLessEqual(len(bins), 7)
        self.assertEqual(sum(item['count'] for item in bins), len(values))
        for price in values:
            matches = [item for item in bins if contains_price(price, item)]
            self.assertEqual(len(matches), 1, f'{price}: {bins}')
        if values:
            self.assertTrue(contains_price(min(values), bins[0]))
            self.assertTrue(contains_price(max(values), bins[-1]))
        return result

    def test_high_and_low_price_samples_have_useful_ranges(self):
        """手机和低价商品各自分段，最高价不会统一挤进无上限末档。"""
        for prices in ([1599,1699,2999,3499,3699,4499,5299,6499,8999,9499,10999],
                       [3.35,3.9,5.8,5.9,6,11.9,14.8,16,19.9,23.5,29.99,39.9,46.55,59.9]):
            with self.subTest(prices=prices):
                result = self.check_partition(prices)
                self.assertEqual(result['mode'], 'equal_width')
                self.assertTrue(5 <= len(result['bins']) <= 7)
                self.assertGreater(sum(item['count'] > 0 for item in result['bins']), 3)
                self.assertEqual(result['bins'][-1]['actual_max'], Decimal(str(max(prices))))

    def test_shared_edges_and_exact_maximum_are_counted_once(self):
        """恰好落在段界和最后上限的价格只计一次，最后区间包含最高价。"""
        result = self.check_partition(range(0, 201, 20))
        bins = result['bins']
        for left, right in zip(bins, bins[1:]):
            self.assertEqual(left['high'], right['low'])
            self.assertFalse(contains_price(left['high'], left))
            self.assertTrue(contains_price(left['high'], right))
        self.assertTrue(bins[-1]['include_high'])
        self.assertEqual(bins[-1]['high'], Decimal(200))

    def test_both_tails_are_retained_and_core_is_not_stretched(self):
        """极端低价、高价单列，主体仍能细分，两个尾部不与主体重复。"""
        result = self.check_partition([0, *range(1000,1801,100),100000])
        self.assertEqual(result['mode'], 'tails')
        self.assertEqual((result['bins'][0]['kind'], result['bins'][-1]['kind']), ('low_tail','high_tail'))
        self.assertEqual((result['bins'][0]['count'], result['bins'][-1]['count']), (1,1))
        core = result['bins'][1:-1]
        self.assertGreater(len(core), 1)
        self.assertTrue(all(item['high'] - item['low'] <= 400 for item in core))
        self.assertEqual(result['bins'][-1]['actual_max'], Decimal(100000))

    def test_empty_same_price_few_prices_and_zero(self):
        """空样本不造分段，同价为一档，少量不同价位按实际价位且保留零元。"""
        self.assertEqual(self.check_partition([])['mode'], 'empty')
        same = self.check_partition([4499] * 15)
        self.assertEqual([(item['label'], item['count']) for item in same['bins']], [('4499元',15)])
        few = self.check_partition([0,0,0.01,99999])
        self.assertEqual(len(few['bins']), 3)
        self.assertEqual(few['bins'][0]['actual_min'], 0)

    def test_zero_interquartile_range_preserves_nonmedian_prices(self):
        """大量同价导致四分位距为零时，中心价和两侧实际价格仍全部保留。"""
        result = self.check_partition([0,100,200,300, *([500]*50),600,700,800,100000])
        self.assertEqual([item['kind'] for item in result['bins']], ['low_tail','point','high_tail'])
        self.assertEqual(result['bins'][1]['count'], 50)

    def test_decimal_precision_and_randomized_coverage(self):
        """微小金额、高低跨度及重复值的确定性样例均不漏计、不重计。"""
        self.check_partition([Decimal('0.000001')*value for value in range(20)])
        rng = random.Random(20261028)
        for _ in range(80):
            scale = Decimal(10) ** rng.randint(-5,6)
            self.check_partition([Decimal(rng.randrange(0,10000)) * scale for _ in range(rng.randrange(1,70))])


class PriceAnalysisIntegrationTests(unittest.TestCase):
    def setUp(self):
        """复用独立数据库并准备分散价格；参数：无；返回：无。"""
        self.fixture = fixtures.SearchAnalysisTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for identifier in range(1,31):
            self.fixture.observation(identifier, title=f'商品{identifier}', price_value=identifier*100,
                                     ad_status='identified' if identifier==30 else 'not_identified')

    def test_detail_filters_and_export_keep_one_price_scheme(self):
        """点击、明细搜索、排序、分页和导出共用全范围分段，交叉表使用相同计数。"""
        original = self.fixture.read()
        item = original['price_bins'][-1]
        selected = {'bin':str(item['index']), 'price_scale':original['filters']['price_scale']}
        result = self.fixture.read(**selected, sort='price_desc', q='商品')
        self.assertEqual(result['price_bins'], original['price_bins'])
        self.assertEqual(len(result['detail_rows']), item['count'])
        self.assertTrue(all(contains_price(row['price'],item) for row in result['detail_rows']))
        self.assertEqual([(i['index'],i['count']) for i in result['price_cross']], [(i['index'],i['count']) for i in original['price_bins']])
        query = '&'.join(key+'='+value for key,value in selected.items())
        page = self.fixture.client.get('/analysis/searches?'+query).get_data(as_text=True)
        self.assertIn('name="price_scale"',page)
        response = self.fixture.client.get('/analysis/searches/export.csv?'+query)
        records = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertIn(item['label'], records[0])
        self.assertIn(item['boundary_label'], records[0])
        self.assertEqual(len(records)-2, item['count'])
        self.assertEqual(self.fixture.read(page='2')['price_bins'], original['price_bins'])

    def test_scope_change_and_legacy_links_clear_stale_bins(self):
        """广告或搜索范围变化及旧固定分段链接均清理旧价格条件，不静默错选。"""
        original = self.fixture.read()
        selected = {'bin':'0','price_scale':original['filters']['price_scale']}
        changed = self.fixture.read(exclude_ads='1', **selected)
        self.assertIsNone(changed['filters']['bin'])
        self.assertTrue(changed['price_filter_reset'])
        self.assertNotEqual(changed['filters']['price_scale'],original['filters']['price_scale'])
        self.assertIsNone(self.fixture.read(bin='1')['filters']['bin'])
        self.assertFalse(self.fixture.read(**selected)['price_filter_reset'])
        invalid = self.fixture.client.get('/analysis/searches?bin=6&price_scale='+original['filters']['price_scale'])
        self.assertEqual(invalid.status_code,400)

    def test_seventh_price_bin_is_selectable(self):
        """自动生成第七段时编号6可筛选且最高价保留，替代旧固定六段上限。"""
        table = self.fixture.tables['list_observations']
        with self.fixture.engine.begin() as connection:
            connection.execute(table.delete().where(table.c.id>8))
            for identifier,price in enumerate(('2.4','4','6','8','10','12','14','16'),start=1):
                connection.execute(table.update().where(table.c.id==identifier).values(price_value=Decimal(price)))
        original = self.fixture.read()
        self.assertEqual(len(original['price_bins']),7)
        selected = self.fixture.read(bin='6',price_scale=original['filters']['price_scale'])
        self.assertEqual([row['price'] for row in selected['detail_rows']],[Decimal(16)])

    def test_ties_and_missing_prices_do_not_invent_one_concentration(self):
        """并列最高档如实说明，缺价样本显示空状态并保留原商品。"""
        table = self.fixture.tables['list_observations']
        with self.fixture.engine.begin() as connection:
            connection.execute(table.update().values(price_value=100))
            connection.execute(table.update().where(table.c.id>15).values(price_value=200))
        result = self.fixture.read()
        self.assertEqual(len(result['peak_bins']),2)
        self.assertIn('2个价位并列最多', self.fixture.client.get('/analysis/searches').get_data(as_text=True))
        with self.fixture.engine.begin() as connection:
            connection.execute(table.update().values(price_value=None))
        result = self.fixture.read()
        self.assertEqual(result['price_bins'],[])
        self.assertEqual(result['stats']['products'],30)
        self.assertEqual(result['peak_bins'],[])
        self.assertIn('暂无有效入口价',self.fixture.client.get('/analysis/searches').get_data(as_text=True))
