"""搜索词分析的真实查询口径回归，样例仅写入临时SQLite。"""
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import copy
import csv
import io
import tempfile
import unittest

import sqlalchemy as sa

from ecommerce.infrastructure.storage.mysql_migrations.schema_v9 import build_metadata
from ecommerce.modules.analysis.service import analysis_page, parse_filters
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app
from ecommerce.web.navigation import safe_return


class SearchAnalysisTests(unittest.TestCase):
    def setUp(self):
        """建立独立库和网页；参数：无；返回：无，不连接真实采集平台。"""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = load_settings(root=Path(folder.name))
        self.app = create_app(base)
        self.settings = replace(base, collection_backend='mysql')
        self.app.config.update(SETTINGS=self.settings, TESTING=True)
        self.client = self.app.test_client()
        metadata = sa.MetaData()
        for table in build_metadata().tables.values():
            columns = []
            for column in table.c:
                kind = copy.copy(column.type)
                if hasattr(kind, 'collation'):
                    kind.collation = None
                columns.append(sa.Column(column.name, kind, nullable=True))
            sa.Table(table.name, metadata, *columns)
        self.engine = sa.create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.tables = metadata.tables
        mock = patch('ecommerce.infrastructure.storage.search_analysis.collection_store',
                     return_value=SimpleNamespace(engine=self.engine, tables=self.tables))
        mock.start()
        self.addCleanup(mock.stop)
        self.task = 'a' * 32
        self.later_task = 'b' * 32
        self.now = datetime(2026, 10, 28, 10)
        self.batch(1)

    def put(self, table, **values):
        """保存本地样例；参数：表和字段；返回：无。"""
        with self.engine.begin() as connection:
            connection.execute(self.tables[table].insert().values(**values))

    def batch(self, identifier, **overrides):
        """创建已发布关键词批次；参数：编号和覆盖值；返回：无。"""
        self.put('collection_batches', **dict(id=identifier, module='list', visibility='published',
            search_method='keyword', search_keyword='粘毛器', task_id=self.task, captured_at=self.now,
            page_number=1, sort_label='销量', requested_limit=40, coverage_status='limit_reached') | overrides)

    def observation(self, identifier, *, product=None, batch=1, **overrides):
        """创建带明确来源的商品观察；参数：编号、商品、批次及字段；返回：无。"""
        if product is None:
            product = identifier
        with self.engine.connect() as connection:
            exists = connection.scalar(sa.select(self.tables['products'].c.id).where(self.tables['products'].c.id == product))
        if not exists and product:
            self.put('products', id=product, platform='taobao', platform_product_id=str(10000000000000 + product))
        values = dict(id=identifier, batch_id=batch, product_id=product or None, slot_index=identifier,
            title='可水洗粘毛器', price_value=Decimal('10'), price_text='¥10', sales_text='1万+人收货',
            sales_count=10000, sales_precision='lower_bound', sales_metric='received_people', sales_period='unknown',
            sales_parse_status='parsed', ad_status='not_identified', shop_name='样例店铺', payload={'currency_symbol': '¥'})
        self.put('list_observations', **(values | overrides))

    def read(self, **filters):
        """调用公开分析入口；参数：筛选；返回：分析结果。"""
        return analysis_page(self.settings, keyword='粘毛器', **filters)

    def test_published_keyword_scope_and_no_silent_page_limit(self):
        """分析完整样本而非网页首屏，暂存、隔离和非关键词批次不可混入。"""
        for identifier in range(1, 26):
            self.observation(identifier)
        for identifier, values in [(2, {'visibility': 'staging'}), (3, {'visibility': 'quarantined'}),
                                   (4, {'search_method': 'image'}), (5, {'search_keyword': '另一搜索词'})]:
            self.batch(identifier, **values)
            self.observation(100 + identifier, batch=identifier)
        result = self.read()
        self.assertEqual(result['stats']['products'], 25)
        self.assertEqual(len(result['table_rows']), 20)
        self.assertEqual(result['pagination']['pages'], 2)
        self.assertEqual(len(result['detail_rows']), 25)
        self.assertEqual(self.read(page='2')['table_rows'][0]['id'], 21)

    def test_deduplicate_before_ad_filter_and_preserve_one_observation(self):
        """单次首次观察、范围最新观察均先确定，再过滤；禁止旧记录补位和跨观察混字段。"""
        self.observation(1, product=1, title='首次标题', price_value=Decimal('3'), ad_status='identified')
        self.observation(2, product=1, title='第二位置', price_value=Decimal('20'))
        self.observation(3, product=2, ad_status=None)
        self.assertEqual(self.read(snapshot='task:' + self.task)['all_rows'][0]['title'], '首次标题')
        result = self.read(snapshot='task:' + self.task, exclude_ads='1')
        self.assertEqual([row['product_id'] for row in result['all_rows']], [2])
        self.assertEqual(result['stats']['excluded_ads'], 1)
        self.batch(2, task_id=self.later_task, captured_at=self.now + timedelta(hours=1))
        self.observation(4, product=2, batch=2, title='最新广告', ad_status='identified')
        latest = self.read(snapshot='range', exclude_ads='1')
        self.assertEqual([row['product_id'] for row in latest['all_rows']], [1])
        self.assertEqual(latest['all_rows'][0]['title'], '第二位置')
        self.assertEqual(latest['all_rows'][0]['price'], Decimal('20'))
        self.assertEqual(latest['stats']['observations'], 2)

    def test_unknown_identities_missing_values_and_zero(self):
        """未知身份分别保留，缺值不补零，精确零仍参与分布。"""
        self.observation(1, product=0, price_value=None, sales_count=None, sales_parse_status='not_provided', sales_precision='unknown')
        self.observation(2, product=0, price_value=0, sales_count=0, sales_precision='exact')
        self.observation(3, payload={'currency_symbol': '$'})
        result = self.read()
        self.assertEqual(result['stats']['products'], 3)
        self.assertEqual(result['stats']['unknown_identities'], 2)
        self.assertEqual(result['stats']['prices'], 1)
        self.assertEqual(result['stats']['price_median'], 0)
        self.assertEqual(result['scatter'][0]['sales_count'], 0)
        self.assertEqual(len(self.read(table_group='missing')['detail_rows']), 1)

    def test_quantity_period_precision_and_threshold_denominators(self):
        """不同口径与周期分组；近似值及未达门槛下界不进入可判断分母。"""
        self.observation(1, sales_count=10000)
        self.observation(2, sales_count=20000, sales_precision='approximate')
        self.observation(3, sales_count=5000)
        self.observation(4, sales_count=0, sales_precision='exact')
        self.observation(5, sales_period='monthly')
        result = self.read(group='received_people:unknown')
        self.assertEqual(len(result['sales_groups']), 2)
        self.assertEqual(result['stats']['comparable'], 4)
        self.assertEqual(result['stats']['high'], 1)
        cross = result['price_cross'][result['all_rows'][0]['price_bin']]
        self.assertEqual(cross['threshold_eligible'], 2)
        self.assertEqual(cross['high_percent'], 50)
        self.assertEqual(len(self.read(finding='high')['detail_rows']), 1)

    def test_empty_data_and_invalid_scope_are_honest(self):
        """空数据正常展示；非法参数和跨搜索词快照明确拒绝。"""
        self.assertEqual(self.client.get('/analysis/searches').status_code, 200)
        for query in ['exclude_ads=yes', 'bin=7', 'price_scale=bad', 'page=0', 'snapshot=bad', 'since=bad',
                      'since=2026-10-29&until=2026-10-28', 'group=unknown:invalid', 'saved=bad', 'finding=bad']:
            self.assertEqual(self.client.get('/analysis/searches?' + query).status_code, 400, query)
        self.assertEqual(self.client.get('/analysis/searches?snapshot=task:' + 'f' * 32).status_code, 404)
        self.app.config['SETTINGS'] = replace(self.settings, collection_backend='files')
        self.assertEqual(self.client.get('/analysis/searches').status_code, 409)

    def test_default_range_uses_all_tasks_and_latest_whole_observation(self):
        """默认和空参数均汇总多次采集取最新，最近一次与具体任务仍可显式选择。"""
        self.observation(1, product=1, title='历史标题', price_value=Decimal('3'))
        self.observation(2, product=2)
        self.batch(2, task_id=self.later_task, captured_at=self.now + timedelta(days=1))
        self.observation(3, product=1, batch=2, title='最新标题', price_value=Decimal('20'))
        for options in ({}, {'snapshot': ''}):
            result = self.read(**options)
            self.assertEqual(result['filters']['snapshot'], 'range')
            self.assertEqual(result['stats']['products'], 2)
            self.assertEqual(result['stats']['source_observations'], 3)
            product = next(row for row in result['all_rows'] if row['product_id'] == 1)
            self.assertEqual((product['title'], product['price']), ('最新标题', Decimal('20')))
        latest = self.read(snapshot='latest')
        self.assertEqual(latest['filters']['snapshot'], 'task:' + self.later_task)
        self.assertEqual(latest['stats']['products'], 1)
        specific = self.read(snapshot='task:' + self.task)
        self.assertEqual(specific['stats']['products'], 2)
        dated = self.read(until='2026-10-28')
        self.assertEqual(dated['filters']['snapshot'], 'range')
        self.assertEqual(next(row for row in dated['all_rows'] if row['product_id'] == 1)['title'], '历史标题')
        text = self.client.get('/analysis/searches?keyword=粘毛器').get_data(as_text=True)
        self.assertIn('<option value="range" selected>', text)

    def test_selected_missing_group_is_visible_and_latest_tie_uses_observation_id(self):
        """有效但当前为空的口径不隐式切换；同时间最新记录以观察编号破同值。"""
        self.observation(1, product=1, slot_index=20, title='旧观察')
        self.observation(2, product=1, slot_index=2, title='最新观察')
        result = self.read(snapshot='range', group='received_people:monthly')
        self.assertEqual(result['all_rows'][0]['title'], '最新观察')
        self.assertFalse(result['group_available'])
        self.assertEqual(result['stats']['comparable'], 0)
        self.assertIn('月度', result['group_label'])
        response = self.client.get('/analysis/searches?group=received_people:monthly&table_group=received_people:monthly')
        self.assertIn('所选口径暂无数据', response.get_data(as_text=True))

    def test_csv_uses_all_filtered_rows_and_protects_cells(self):
        """导出与页面同口径，不受分页截断，长编号和公式式标题安全。"""
        for identifier in range(1, 26):
            self.observation(identifier, ad_status='identified' if identifier == 1 else None,
                             title='=HYPERLINK("https://example.test")' if identifier == 2 else '可水洗粘毛器')
        response = self.client.get('/analysis/searches/export.csv?keyword=粘毛器&exclude_ads=1')
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(len(rows), 26)
        self.assertIn('排除已识别广告', rows[0])
        self.assertEqual(rows[2][0], '2')
        self.assertTrue(rows[2][3].startswith("'="))
        self.assertEqual(rows[2][2], "'10000000000002")
        self.assertNotIn('已识别广告', [row[14] for row in rows[2:]])
        self.assertIn('attachment', response.headers['Content-Disposition'])

    def test_template_escapes_and_links_to_same_source(self):
        """标题和店名转义，图表JSON不允许闭合脚本，来源链接保持观察归属。"""
        self.observation(1, title='</script><script>alert(1)</script>', shop_name='<b>店铺</b>')
        response = self.client.get('/analysis/searches?keyword=粘毛器')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('<script>alert(1)</script>', text)
        self.assertIn('/collections/batches/1?', text)
        self.assertIn('observation=1', text)
        self.assertIn('sa-exclude-ads', text)
        self.assertEqual(safe_return('/analysis/searches?keyword=a'), '/analysis/searches?keyword=a')
        self.assertEqual(safe_return('//evil.test/analysis/searches'), '/collections')

    def test_dates_standalone_batches_and_selection_do_not_cross_keywords(self):
        """日期包含结束日；无任务批次单独选择，同任务不同搜索词不能混合。"""
        self.observation(1)
        self.batch(2, task_id=None, captured_at=self.now - timedelta(days=1))
        self.observation(2, batch=2)
        self.batch(3, search_keyword='手机')
        self.observation(3, batch=3)
        self.assertEqual(self.read(snapshot='batch:2')['stats']['products'], 1)
        result = self.read(since='2026-10-28', until='2026-10-28')
        self.assertEqual([row['id'] for row in result['all_rows']], [1])
        with self.assertRaises(LookupError):
            self.read(snapshot='batch:2', since='2026-10-28')

    def test_store_identity_name_fallback_and_no_source_writes(self):
        """公开路由优先；同名近似分组有标记，未知店铺不合并；所有分析均只读。"""
        self.observation(1, payload={'shop_url': 'https://shop123.taobao.com/?spm=tracking'})
        self.observation(2, payload={'shop_url': 'https://shop123.taobao.com/'})
        self.observation(3)
        self.observation(4, shop_name=None)
        with self.engine.connect() as connection:
            before = [dict(row) for row in connection.execute(sa.select(self.tables['list_observations'])).mappings()]
        result = self.read(view='stores')
        self.assertEqual(result['stats']['shops'], 2)
        self.assertEqual(result['stats']['missing_shops'], 1)
        self.assertEqual(result['shops'][0]['count'], 2)
        self.assertEqual(result['shops'][1]['basis'], '按店铺名称近似分组')
        self.client.get('/analysis/searches?view=stores')
        with self.engine.connect() as connection:
            after = [dict(row) for row in connection.execute(sa.select(self.tables['list_observations'])).mappings()]
        self.assertEqual(before, after)

    def test_saved_list_obeys_ads_without_product_classification(self):
        """不再推测产品类型；关注商品继续服从广告排除。"""
        self.observation(1, title='粘毛滚筒手柄替换芯套装', ad_status='identified')
        self.observation(2, title='手机保护壳')
        result = self.read()
        self.assertNotIn('category', result['all_rows'][0])
        self.assertNotIn('categories', result)
        self.assertNotIn('unclassified', result['stats'])
        self.assertEqual(self.read(view='saved', saved='p:1')['pagination']['total'], 1)
        self.assertEqual(self.read(view='saved', saved='p:1', exclude_ads='1')['pagination']['total'], 0)

    def test_product_type_removed_from_page_store_and_exports(self):
        """商品、店铺、依据数据和两类CSV不再输出产品类型。"""
        self.observation(1)
        result = self.read(category='替换耗材', finding='unclassified')
        self.assertEqual(result['stats']['products'], 1)
        self.assertNotIn('category', result['filters'])
        self.assertNotIn('categories', result['shops'][0])
        for suffix in ('', '?view=stores'):
            response = self.client.get('/analysis/searches' + suffix)
            text = response.get_data(as_text=True)
            for retired in ('产品类型', '类型初分', '标题初分', '待分类', '分类待核实', 'sa-type-row'):
                self.assertNotIn(retired, text)
            response = self.client.get('/analysis/searches/export.csv' + suffix)
            rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
            self.assertNotIn('类型', rows[0])
            self.assertNotIn('标题初分类型', rows[1])
            self.assertEqual(len(rows[1]), len(rows[2]))

    def test_retired_category_urls_clear_only_retired_filters(self):
        """旧类型链接与导出自动清理参数，其余搜索词、日期和广告筛选继续保留。"""
        self.observation(1, ad_status='identified')
        self.observation(2)
        for path in ('/analysis/searches', '/analysis/searches/export.csv'):
            response = self.client.get(path + '?keyword=粘毛器&category=替换耗材&finding=unclassified&exclude_ads=1&since=2026-10-28')
            self.assertEqual(response.status_code, 302)
            location = response.headers['Location']
            self.assertNotIn('category=', location)
            self.assertNotIn('finding=', location)
            self.assertIn('exclude_ads=1', location)
            self.assertIn('since=2026-10-28', location)
            clean = self.client.get(location)
            self.assertEqual(clean.status_code, 200)
        text = self.client.get('/analysis/searches?category=bad&finding=unclassified', follow_redirects=True).get_data(as_text=True)
        self.assertIn('样本商品 / 独立观察', text)


if __name__ == '__main__':
    unittest.main()
