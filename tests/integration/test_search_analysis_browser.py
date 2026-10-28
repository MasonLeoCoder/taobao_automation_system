"""搜索词分析的隔离网页验证；所有请求进入临时Flask，不访问真实平台。"""
import os
from datetime import timedelta
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from tests.unit import test_search_analysis as fixtures


@unittest.skipUnless(os.environ.get('ANALYSIS_BROWSER_TESTS') == '1', '显式启用本地分析页浏览器验证')
class SearchAnalysisBrowserTests(unittest.TestCase):
    def setUp(self):
        """创建临时数据及隔离浏览器；参数：无；返回：无。"""
        self.fixture = fixtures.SearchAnalysisTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.observation(1, title='可水洗粘毛器广告样例', ad_status='identified')
        self.fixture.observation(2, title='可水洗粘毛器普通样例', price_value=0, sales_count=0, sales_precision='exact')
        self.fixture.observation(3, title='替换粘毛纸补充装', price_value=5, sales_period='monthly')
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context(viewport={'width': 1480, 'height': 1050}, accept_downloads=True)
        self.context.route('**/*', self.route)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def route(self, route):
        """限定全部网络至临时应用；参数：浏览器请求；返回：本地响应。"""
        url = urlsplit(route.request.url)
        if url.hostname != '127.0.0.1':
            route.abort()
            return
        if url.path == '/api/collection-jobs/active':
            route.fulfill(json={'jobs': []})
            return
        response = self.fixture.client.get(url.path + ('?' + url.query if url.query else ''))
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
        response.close()

    def goto(self):
        """打开隔离分析页面；参数：无；返回：无。"""
        self.page.goto('http://127.0.0.1/analysis/searches')
        expect(self.page.locator('#sa-scatter svg')).to_be_visible()

    def test_ad_filter_recalculates_charts_and_export(self):
        """排除广告同步更新概况、图表、清单和导出，不丢失零值。"""
        self.goto()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(3)
        self.page.locator('#sa-exclude-ads').check()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(2)
        expect(self.page.locator('.sa-ad-row')).to_contain_text('已排除 1 件')
        expect(self.page.locator('#sa-scatter circle')).to_have_count(1)
        expect(self.page.locator('.sa-products tbody')).to_contain_text('¥0.00')
        self.assertNotIn('广告样例', self.page.locator('.sa-products tbody').inner_text())
        self.assertIn('exclude_ads=1', self.page.locator('#sa-export').get_attribute('href'))
        self.assertEqual(self.errors, [])

    def test_drawer_focus_saved_list_and_chart_keyboard(self):
        """依据侧栏安全展示，关注跨刷新保留，散点支持键盘打开。"""
        self.goto()
        self.page.locator('.sa-detail-btn').first.click()
        expect(self.page.locator('#sa-detail-dialog')).to_be_visible()
        expect(self.page.locator('#sa-detail-content')).to_contain_text('观察编号')
        self.page.locator('.sa-drawer-save').click()
        expect(self.page.locator('#sa-saved-count')).to_have_text('1')
        self.page.locator('[data-sa-close="sa-detail-dialog"]').click()
        self.page.locator('#sa-saved-link').click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('.sa-products tbody')).to_contain_text('广告样例')
        self.page.locator('#sa-exclude-ads').check()
        expect(self.page.locator('.sa-products tbody')).not_to_contain_text('广告样例')
        self.page.wait_for_load_state('networkidle')
        with self.page.expect_navigation():
            self.page.locator('#sa-group-select').select_option('received_people:last_30_days')
        dot = self.page.locator('#sa-scatter circle[data-analysis-point="2"]')
        dot.focus()
        dot.press('Enter')
        expect(self.page.locator('#sa-detail-dialog')).to_be_visible()
        expect(self.page.locator('#sa-detail-title')).to_contain_text('普通样例')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('#sa-detail-dialog')).not_to_be_visible()
        self.assertEqual(self.errors, [])

    def test_bin_drilldown_group_and_store_view(self):
        """价格区间、数量口径和店铺视图保持分析范围，所有跳转可用。"""
        self.goto()
        self.page.get_by_role('link', name='5元，1项商品，筛选明细', exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('.sa-active-filters')).to_contain_text('5元')
        self.page.get_by_role('link', name='清除明细筛选 ×', exact=True).click()
        self.page.wait_for_load_state('networkidle')
        with self.page.expect_navigation():
            self.page.locator('#sa-group-select').select_option('received_people:monthly')
        expect(self.page.locator('#sa-scatter circle')).to_have_count(1)
        self.page.get_by_role('link', name='店铺分布', exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        self.page.get_by_role('link', name='查看商品 ↗', exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(3)
        self.assertEqual(self.errors, [])

    def test_received_period_30_days_is_consistent_and_old_group_is_visible(self):
        """近30天分组、明细和旧链接同步，并向用户说明周期更正。"""
        self.page.goto('http://127.0.0.1/analysis/searches?group=received_people:unknown&table_group=received_people:unknown')
        expect(self.page.locator('#sa-group-select')).to_have_value('received_people:last_30_days')
        expect(self.page.locator('#sa-group-select option:checked')).to_contain_text('收货人数 · 近30天')
        expect(self.page.get_by_text('收货人数周期已更正为近30天，已同步更新该数量口径筛选。',exact=True)).to_be_visible()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(2)
        expect(self.page.locator('.sa-products tbody')).to_contain_text('近30天')
        self.assertEqual(self.errors, [])

    def test_mobile_layout_has_only_table_horizontal_scroll(self):
        """窄屏整页不溢出，表格可独立横向滚动，分析口径可阅读。"""
        self.goto()
        for width in (390, 768, 1480):
            self.page.set_viewport_size({'width': width, 'height': 1000})
            size = self.page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})')
            self.assertLessEqual(size['scroll'], size['width'] + 1, str(size))
        self.page.locator('#sa-rules-open').click()
        expect(self.page.locator('#sa-rules-dialog')).to_be_visible()
        expect(self.page.locator('.sa-rules-body')).to_contain_text('先确定商品观察，再排除广告')
        self.assertEqual(self.errors, [])

    def test_concentration_top50_metrics_query_export_and_original_scopes(self):
        """前50排序和指标一致，前三跳转清理旧条件，查询/CSV沿用名单，原模块不缩小。"""
        for i in range(4,64): self.fixture.observation(i,sales_count=i*100,price_value=None if i%2 else i)
        self.goto()
        concentration=self.page.locator('#sa-concentration')
        self.assertIsNone(concentration.get_attribute('open'))
        concentration.locator(':scope > summary').click()
        expect(concentration.locator(':scope > summary')).to_contain_text('50')
        expect(concentration.locator('[data-concentration-metric="HHI"]')).to_contain_text('200.00')
        self.page.locator('#sa-exclude-ads').check()
        concentration.locator(':scope > summary').click()
        concentration.locator('[data-concentration-link="top3"]').click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(3)
        expect(self.page.locator('.sa-products tbody tr').first).to_have_attribute('id','analysis-observation-63')
        expect(self.page.locator('.sa-active-filters')).to_contain_text('集中度入选')
        self.page.get_by_label('搜索明细',exact=True).fill('粘毛器')
        self.page.get_by_role('button',name='查询明细',exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(3)
        self.assertIn('finding=top3',self.page.locator('#sa-export').get_attribute('href'))
        self.assertEqual(sum(int(x) for x in self.page.locator('[data-price-bin] svg text').all_text_contents()),32)
        self.page.locator('[data-price-bin]').first.click()
        self.assertNotIn('finding=top3',self.page.url)
        self.assertEqual(self.errors,[])

    def test_concentration_empty_zero_small_and_mobile(self):
        """不足3/10件和全零均明确提示，手机展开依据时只表内滚动。"""
        self.goto()
        concentration=self.page.locator('#sa-concentration')
        concentration.locator(':scope > summary').click()
        expect(concentration).to_contain_text('有效商品不足50件')
        expect(concentration.locator('[data-concentration-metric="CR3"]')).to_contain_text('样本不足3件')
        self.page.locator('#sa-exclude-ads').check()
        concentration.locator(':scope > summary').click()
        expect(concentration.locator('[data-concentration-metric="HHI"]')).to_contain_text('人数计算值合计为0')
        concentration.locator('.sa-concentration-basis > summary').click()
        for width in (1480,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.page.goto('http://127.0.0.1/analysis/searches?keyword=粘毛器&since=2099-01-01')
        concentration.locator(':scope > summary').click()
        expect(concentration.locator('[data-concentration-metric="HHI"]')).to_contain_text('暂无可计算商品')
        self.assertEqual(self.errors,[])

    def test_shipping_regions_fold_city_click_and_clear(self):
        """省份和城市分别折叠，查看商品包含缺价，清除恢复全部；其他图表保持原样。"""
        self.fixture.observation(4,location_text='浙江 金华',price_value=None)
        self.fixture.observation(5,location_text='浙江 嘉兴')
        self.goto()
        region=self.page.locator('#sa-shipping-regions')
        self.assertIsNone(region.get_attribute('open'))
        expect(region.locator('summary').first).to_contain_text('地区已记录 2／5 件')
        region.locator(':scope > summary').click()
        province=region.locator('[data-region-key="浙江"]')
        expect(province.locator(':scope > summary')).to_contain_text('40.0%')
        province.locator(':scope > summary').click()
        expect(province.locator('.sa-region-cities')).to_contain_text('以该地区 2 件商品为分母')
        expect(province.locator('.sa-region-cities')).to_contain_text('50.0%')
        self.assertIsNone(self.page.locator('#sa-price-composition').get_attribute('open'))
        province.locator('[data-city-link="金华"]').click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('.sa-active-filters')).to_contain_text('浙江 / 金华')
        self.page.get_by_label('搜索明细',exact=True).fill('粘毛器')
        self.page.get_by_role('button',name='查询明细',exact=True).click()
        expect(self.page.locator('#sa-detail-form input[name="region"]')).to_have_value('浙江')
        expect(self.page.locator('#sa-detail-form input[name="city"]')).to_have_value('金华')
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        self.assertIn('region=',self.page.locator('#sa-export').get_attribute('href'))
        self.assertIn('city=',self.page.locator('#sa-export').get_attribute('href'))
        self.page.get_by_role('link',name='清除明细筛选 ×',exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(5)
        self.assertNotIn('region=',self.page.url)
        self.assertEqual(self.errors,[])

    def test_shipping_region_mobile_filters_and_other_drilldowns(self):
        """窄屏无页面溢出，地区点击清理旧查询，价格柱重新选择时清理地区。"""
        self.fixture.observation(4,location_text='浙江 金华')
        self.goto()
        for width in (1480,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            region=self.page.locator('#sa-shipping-regions')
            if region.get_attribute('open') is None: region.locator(':scope > summary').click()
            region.locator('[data-region-key="浙江"] > summary').click()
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.page.get_by_label('搜索明细',exact=True).fill('不存在的词')
        self.page.get_by_role('button',name='查询明细',exact=True).click()
        region.locator(':scope > summary').click()
        region.locator('[data-region-link="浙江"]').click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        self.page.locator('[data-price-bin]').first.click()
        self.assertNotIn('region=',self.page.url)
        self.assertEqual(self.errors,[])

    def test_common_received_ties_missing_price_and_clear(self):
        """并列档位独立点击，缺价不丢失，清除筛选和CSV保持数量一致。"""
        self.fixture.observation(4,price_value=None,sales_count=0,sales_precision='exact')
        self.fixture.observation(5,sales_count=10000,sales_precision='lower_bound')
        self.goto()
        card=self.page.locator('#sa-common-quantity')
        expect(card).to_contain_text('2个档位并列最多')
        expect(card.locator('[data-common-tier]')).to_have_count(2)
        expect(card).to_contain_text('占 50.0%')
        card.locator('[data-common-tier$=":exact:0"]').click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(2)
        expect(self.page.locator('.sa-active-filters')).not_to_contain_text('达到1万')
        self.assertIn('finding=quantity',self.page.locator('#sa-export').get_attribute('href'))
        self.page.get_by_role('link',name='清除明细筛选 ×',exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(5)
        self.assertNotIn('quantity_tier=',self.page.url)
        for width in (1480,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.assertEqual(self.errors,[])

    def test_common_received_updates_with_ad_filter(self):
        """排除广告后卡片重算分母和档位，点击不继承旧的明细搜索。"""
        self.goto()
        self.page.locator('#sa-exclude-ads').check()
        card=self.page.locator('#sa-common-quantity')
        expect(card.locator('[data-common-tier]')).to_have_count(1)
        expect(card).to_contain_text('按有近30天收货人数的 1 件商品统计')
        expect(card).to_contain_text('100.0%')
        self.page.get_by_label('搜索明细',exact=True).fill('不存在的商品')
        self.page.get_by_role('button',name='查询明细',exact=True).click()
        card.locator('[data-common-tier]').click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('.sa-products tbody')).to_contain_text('普通样例')
        self.assertEqual(self.errors,[])

    def test_price_composition_style_counts_and_tier_drilldown(self):
        """展示档位、合计及色标一致，点击按价位和精度筛选，原图表保持完整样本。"""
        self.fixture.observation(4,price_value=0,sales_text='0+人收货',sales_count=0,sales_precision='lower_bound')
        self.goto()
        self.page.locator('#sa-price-composition summary').click()
        expect(self.page.locator('#sa-price-composition')).not_to_contain_text('可判断1万门槛')
        footer=self.page.locator('.sa-composition-table tfoot')
        expect(footer.locator('.sa-composition-count')).to_have_text('4')
        exact=self.page.locator('.sa-composition-table tbody a[data-quantity-tier="received_people:last_30_days:exact:0"]')
        lower=self.page.locator('.sa-composition-table tbody a[data-quantity-tier="received_people:last_30_days:lower_bound:0"]')
        expect(exact).to_have_count(1);expect(lower).to_have_count(1)
        exact.click()
        self.page.wait_for_url('**#sa-products')
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('.sa-products tbody')).to_contain_text('普通样例')
        expect(self.page.locator('.sa-active-filters')).to_contain_text('展示档位')
        self.assertEqual(sum(int(x) for x in self.page.locator('[data-price-bin] svg text').all_text_contents()),4)
        self.page.get_by_label('搜索明细',exact=True).fill('普通')
        self.page.get_by_role('button',name='查询明细',exact=True).click()
        expect(self.page.locator('#sa-detail-form input[name="quantity_tier"]')).to_have_value('received_people:last_30_days:exact:0')
        self.page.get_by_role('link',name='清除明细筛选 ×',exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(4)
        self.assertNotIn('quantity_tier=',self.page.url)
        self.assertEqual(self.errors, [])

    def test_price_composition_large_tiers_and_mobile_scroll(self):
        """1000+/10万+实际档位和边界提示保留，窄屏只在表格内横向滚动。"""
        self.fixture.observation(4,price_value=50,sales_count=100000,sales_text='10万+人收货')
        self.goto()
        self.page.locator('#sa-price-composition summary').click()
        expect(self.page.locator('.sa-composition-table tfoot')).to_contain_text('10万+人')
        for width in (1480,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.assertTrue(self.page.locator('.sa-composition-price').first.get_attribute('title'))
        tier=self.page.locator('.sa-composition-table tfoot a[data-quantity-tier="received_people:last_30_days:lower_bound:100000"]')
        tier.click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('.sa-products tbody')).to_contain_text('10万+人收货')
        self.assertEqual(self.errors, [])

    def test_scope_and_quantity_filters_do_not_inherit_detail_anchor(self):
        """桌面/手机从明细定位进入，应用和广告回概览，口径切换留图表，明细操作定位列表。"""
        for width in (1480,390):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.page.goto('http://127.0.0.1/analysis/searches#sa-products')
            self.page.get_by_role('button',name='应用筛选',exact=True).click()
            self.page.wait_for_url('**#sa-overview')
            expect(self.page.locator('#sa-overview')).to_be_in_viewport()
            self.page.locator('[data-price-bin]').first.click()
            self.page.wait_for_url('**#sa-products')
            expect(self.page.locator('#sa-products')).to_be_in_viewport()
            self.page.locator('#sa-exclude-ads').check()
            self.page.wait_for_url('**#sa-overview')
            expect(self.page.locator('#sa-overview')).to_be_in_viewport()
            expect(self.page.locator('.sa-ad-row')).to_contain_text('当前样本 2 件')
            self.page.goto('http://127.0.0.1/analysis/searches#sa-products')
            self.page.locator('#sa-group-select').select_option('received_people:monthly')
            self.page.wait_for_url('**#sa-quantity-analysis')
            expect(self.page.locator('#sa-quantity-analysis')).to_be_in_viewport()
            self.page.get_by_label('搜索明细',exact=True).fill('替换')
            self.page.get_by_role('button',name='查询明细',exact=True).click()
            self.page.wait_for_url('**#sa-products')
            expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        self.assertEqual(self.errors, [])

    def test_analysis_pagination_keeps_detail_anchor(self):
        """分析下一页和页码跳转定位明细，共用分页的其他页面保留原地址。"""
        for identifier in range(4,24):
            self.fixture.observation(identifier)
        self.goto()
        pager=self.page.locator('.numbered-pagination')
        pager.get_by_role('link',name='下一页',exact=True).click()
        self.page.wait_for_url('**#sa-products')
        expect(self.page.locator('#sa-products')).to_be_in_viewport()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(3)
        pager.get_by_label('跳转页码',exact=True).fill('1')
        pager.get_by_role('button',name='跳转',exact=True).click()
        self.page.wait_for_function("new URL(location.href).searchParams.get('page')==='1'")
        self.assertEqual(urlsplit(self.page.url).fragment,'sa-products')
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(20)
        from flask import render_template_string
        with self.fixture.app.test_request_context('/collections/details'):
            default_pager=render_template_string("{% from 'pagination.html' import pager with context %}{{ pager(info) }}",info={'page':1,'pages':2,'total':23})
        self.assertIn('href="/collections/details?page=2"',default_pager)
        self.assertNotIn('#sa-products',default_pager)
        self.assertEqual(self.errors, [])

    def test_adaptive_high_prices_tooltip_drilldown_and_detail_search(self):
        """高价样本形成多个价位；提示、点击、明细搜索及导出保留同一分段。"""
        prices = [1599,1699,2999,3499,3699,4499,5299,6499,8999,9499,10999]
        for identifier, price in enumerate(prices, start=1):
            if identifier <= 3:
                table = self.fixture.tables['list_observations']
                with self.fixture.engine.begin() as connection:
                    connection.execute(table.update().where(table.c.id==identifier).values(price_value=price))
            else:
                self.fixture.observation(identifier, price_value=price)
        self.goto()
        expect(self.page.locator('.sa-price-summary')).to_contain_text('¥1599.00')
        expect(self.page.locator('.sa-price-summary')).to_contain_text('¥10999.00')
        bars = self.page.locator('[data-price-bin]')
        self.assertTrue(5 <= bars.count() <= 7)
        original = bars.locator('span:not(.sa-visually-hidden)').all_text_contents()
        bar = bars.last
        bar.hover()
        expect(self.page.locator('#sa-tooltip')).to_be_visible()
        expect(self.page.locator('#sa-tooltip')).to_contain_text('实际价格')
        bar.focus()
        expect(self.page.locator('#sa-tooltip')).to_contain_text('¥10999.00')
        bar.press('Enter')
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('.sa-products tbody')).to_contain_text('10999')
        selected = self.page.url
        self.page.get_by_label('搜索明细', exact=True).fill('不存在的商品')
        self.page.get_by_role('button',name='查询明细',exact=True).click()
        expect(self.page.locator('.sa-empty')).to_contain_text('当前条件下没有数据')
        self.assertEqual(bars.locator('span:not(.sa-visually-hidden)').all_text_contents(), original)
        self.assertIn('bin=', self.page.url)
        self.assertIn('price_scale=', self.page.url)
        self.page.goto(selected)
        with self.page.expect_download() as pending:
            self.page.locator('#sa-export').click()
        download = pending.value
        self.assertIsNone(download.failure())
        self.assertEqual(self.errors, [])

    def test_adaptive_tail_same_price_empty_and_mobile_layout(self):
        """主体和尾部一起保留，窄屏无整页溢出，同价和缺价显示真实状态。"""
        prices = [0,*range(1000,1801,100),100000]
        table = self.fixture.tables['list_observations']
        for identifier, price in enumerate(prices,start=1):
            if identifier <= 3:
                with self.fixture.engine.begin() as connection:
                    connection.execute(table.update().where(table.c.id==identifier).values(price_value=price))
            else:
                self.fixture.observation(identifier,price_value=price)
        self.goto()
        expect(self.page.locator('.sa-price-tail')).to_have_count(2)
        expect(self.page.locator('.sa-price-chart')).to_contain_text('高价尾部')
        self.page.locator('.sa-price-tail').last.click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('.sa-products tbody')).to_contain_text('100000')
        for width in (390,768,1480):
            self.page.set_viewport_size({'width':width,'height':1000})
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        with self.fixture.engine.begin() as connection:
            connection.execute(table.update().values(price_value=4499))
        self.page.goto('http://127.0.0.1/analysis/searches')
        expect(self.page.locator('[data-price-bin]')).to_have_count(1)
        expect(self.page.locator('.sa-price-foot')).to_contain_text('全部有效入口价相同')
        with self.fixture.engine.begin() as connection:
            connection.execute(table.update().values(price_value=None))
        self.page.reload()
        expect(self.page.locator('.sa-price-empty')).to_contain_text('暂无有效入口价')
        expect(self.page.locator('[data-price-bin]')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_price_selection_clears_on_scope_change_and_old_link(self):
        """广告、搜索词与旧链接更新范围时不继续应用旧的价格段编号。"""
        self.add_keywords('手机壳')
        self.goto()
        self.page.locator('[data-price-bin]').last.click()
        expect(self.page.locator('.sa-active-filters')).to_be_visible()
        self.page.locator('#sa-exclude-ads').check()
        expect(self.page.locator('#sa-exclude-ads')).to_be_checked()
        expect(self.page.locator('input[name="bin"]')).to_have_count(0)
        self.page.locator('.ss-trigger').click()
        self.page.get_by_role('combobox',name='查找搜索词',exact=True).fill('手机')
        self.page.locator('.ss-list').get_by_role('option',name='手机壳',exact=True).click()
        self.page.get_by_role('button',name='应用筛选',exact=True).click()
        expect(self.page.locator('[data-price-bin]')).to_have_count(1)
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        self.assertNotIn('bin=',self.page.url)
        self.page.goto('http://127.0.0.1/analysis/searches?keyword=粘毛器&bin=5')
        expect(self.page.get_by_text('样本范围或价格分段已变化，已恢复全部价位。',exact=True)).to_be_visible()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(3)
        self.assertEqual(self.errors, [])

    def test_default_range_and_keyword_date_changes_match_summary(self):
        """默认合并多次采集，切词及改日期恢复最新商品汇总，单次选择仍可使用。"""
        self.fixture.batch(2, task_id=self.fixture.later_task, captured_at=self.fixture.now + timedelta(days=1))
        self.fixture.observation(4, batch=2, title='可水洗粘毛器新增样例')
        self.fixture.observation(5, batch=2, product=1, title='可水洗粘毛器最新标题')
        self.fixture.batch(3, search_keyword='手机壳')
        self.fixture.observation(6, batch=3, title='手机保护壳')
        self.page.goto('http://127.0.0.1/analysis/searches?keyword=%E7%B2%98%E6%AF%9B%E5%99%A8')
        expect(self.page.locator('#sa-scatter svg')).to_be_visible()
        expect(self.page.locator('#sa-snapshot')).to_have_value('range')
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(4)
        with self.page.expect_navigation():
            self.page.locator('#sa-snapshot').select_option('latest')
            self.page.get_by_role('button', name='应用筛选', exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(2)
        self.page.locator('input[name="until"]').fill('2026-10-28')
        expect(self.page.locator('#sa-snapshot')).to_have_value('range')
        self.page.get_by_role('button', name='应用筛选', exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(3)
        self.page.locator('.ss-trigger').click()
        self.page.get_by_role('combobox', name='查找搜索词', exact=True).fill('手机')
        self.page.locator('.ss-list').get_by_role('option', name='手机壳', exact=True).click()
        expect(self.page.locator('#sa-snapshot')).to_have_value('range')
        self.page.get_by_role('button', name='应用筛选', exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('#sa-snapshot')).to_have_value('range')
        self.assertEqual(self.errors, [])

    def add_keywords(self, *words):
        """为选择控件准备独立候选；参数：搜索词；返回：无，仅读写临时样例。"""
        for identifier, word in enumerate(words, start=2):
            self.fixture.batch(identifier, search_keyword=word)
            self.fixture.observation(1000 + identifier, batch=identifier, title=word + '样例商品')

    def test_keyword_search_highlight_select_and_apply(self):
        """输入模糊匹配并高亮，点击只选词，应用后使用最新商品汇总。"""
        self.add_keywords('手机', '手机壳', '清洁工具')
        self.page.goto('http://127.0.0.1/analysis/searches?keyword=%E7%B2%98%E6%AF%9B%E5%99%A8')
        expect(self.page.locator('#sa-scatter svg')).to_be_visible()
        original = self.page.url
        self.page.locator('.ss-trigger').click()
        search = self.page.get_by_role('combobox', name='查找搜索词', exact=True)
        expect(search).to_be_focused()
        search.fill('手机')
        expect(self.page.locator('.ss-list').get_by_role('option')).to_have_count(2)
        expect(self.page.locator('.ss-list mark')).to_have_text(['手机', '手机'])
        expect(self.page.locator('.ss-result-status')).to_have_text('匹配 2 / 4 个搜索词')
        self.page.locator('.ss-list').get_by_role('option', name='手机壳', exact=True).click()
        expect(self.page.locator('.ss-trigger')).to_contain_text('手机壳')
        expect(self.page.locator('.ss-trigger')).to_have_attribute('aria-expanded', 'false')
        self.assertEqual(self.page.url, original)
        expect(self.page.locator('#sa-snapshot')).to_have_value('range')
        with self.page.expect_navigation():
            self.page.get_by_role('button', name='应用筛选', exact=True).click()
        expect(self.page.locator('.ss-trigger')).to_contain_text('手机壳')
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('#sa-snapshot')).to_have_value('range')
        self.assertEqual(self.errors, [])

    def test_keyword_empty_clear_escape_and_keyboard(self):
        """无匹配及清空不改变已选词，Esc还原焦点，方向键和回车可选择。"""
        self.add_keywords('手机', '手机壳')
        self.page.goto('http://127.0.0.1/analysis/searches?keyword=%E7%B2%98%E6%AF%9B%E5%99%A8')
        expect(self.page.locator('#sa-scatter svg')).to_be_visible()
        trigger = self.page.locator('.ss-trigger')
        trigger.click()
        search = self.page.get_by_role('combobox', name='查找搜索词', exact=True)
        search.fill('不存在的搜索词')
        expect(self.page.locator('.ss-list').get_by_role('option')).to_have_count(0)
        expect(self.page.locator('.ss-empty')).to_be_visible()
        search.press('Enter')
        expect(trigger).to_have_attribute('aria-expanded', 'true')
        self.page.get_by_role('button', name='清空搜索', exact=True).click()
        expect(self.page.locator('.ss-list').get_by_role('option')).to_have_count(3)
        expect(self.page.locator('.ss-list').get_by_role('option', selected=True)).to_have_text('#粘毛器')
        search.fill('手机')
        search.press('Tab')
        expect(self.page.get_by_role('button', name='清空搜索', exact=True)).to_be_focused()
        self.page.keyboard.press('Enter')
        expect(search).to_be_focused()
        expect(self.page.locator('.ss-list').get_by_role('option')).to_have_count(3)
        search.press('Escape')
        expect(trigger).to_be_focused()
        expect(trigger).to_have_attribute('aria-expanded', 'false')
        trigger.press('ArrowDown')
        search.fill('手机')
        search.press('ArrowDown')
        search.press('Enter')
        expect(trigger).to_contain_text('手机')
        self.assertEqual(self.page.locator('#sa-keyword').input_value(), '手机')
        self.assertEqual(self.errors, [])

    def test_keyword_many_options_mobile_and_safe_text(self):
        """大量候选可滚动，特殊文字不执行HTML，手机面板不越界或被卡片裁切。"""
        self.add_keywords(*['测试关键词' + str(index).zfill(3) for index in range(220)], '<img src=x onerror=window.badKeyword=1>')
        self.page.goto('http://127.0.0.1/analysis/searches?keyword=%E7%B2%98%E6%AF%9B%E5%99%A8')
        expect(self.page.locator('#sa-scatter svg')).to_be_visible()
        self.page.locator('.ss-trigger').click()
        expect(self.page.locator('.ss-list').get_by_role('option')).to_have_count(222)
        size = self.page.locator('.ss-list').evaluate('(n)=>({scroll:n.scrollHeight,height:n.clientHeight})')
        self.assertGreater(size['scroll'], size['height'])
        search = self.page.get_by_role('combobox', name='查找搜索词', exact=True)
        search.fill('<img')
        expect(self.page.locator('.ss-list').get_by_role('option')).to_have_count(1)
        expect(self.page.locator('.ss-list img')).to_have_count(0)
        self.assertIsNone(self.page.evaluate('window.badKeyword'))
        for height in (420, 900):
            search.press('Escape')
            self.page.set_viewport_size({'width':390, 'height':height})
            self.page.locator('.ss-trigger').click()
            bounds = self.page.locator('.ss-popover').bounding_box()
            self.assertGreaterEqual(bounds['x'], 8)
            self.assertGreaterEqual(bounds['y'], 0)
            self.assertLessEqual(bounds['x'] + bounds['width'], 391)
            self.assertLessEqual(bounds['y'] + bounds['height'], height)
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.assertEqual(self.errors, [])

    def test_keyword_chinese_composition_does_not_select_early(self):
        """中文输入组合过程中回车不提前选中，组合结束后可正常确认。"""
        self.add_keywords('手机壳')
        self.page.goto('http://127.0.0.1/analysis/searches?keyword=%E7%B2%98%E6%AF%9B%E5%99%A8')
        expect(self.page.locator('#sa-scatter svg')).to_be_visible()
        trigger = self.page.locator('.ss-trigger')
        trigger.click()
        search = self.page.get_by_role('combobox', name='查找搜索词', exact=True)
        search.dispatch_event('compositionstart')
        search.fill('手机壳')
        search.press('Enter')
        expect(trigger).to_have_attribute('aria-expanded', 'true')
        self.assertEqual(self.page.locator('#sa-keyword').input_value(), '粘毛器')
        search.dispatch_event('compositionend')
        expect(self.page.locator('.ss-list').get_by_role('option')).to_have_count(1)
        search.press('Enter')
        expect(trigger).to_contain_text('手机壳')
        self.assertEqual(self.errors, [])

    def test_product_type_removed_and_legacy_filter_does_not_hide_goods(self):
        """移除类型筛选、商品/店铺列及依据标签，旧链接清理后仍按广告条件展示。"""
        # 隔离路由仅模拟单次响应，先校验服务端跳转，再打开其真实目标。
        legacy = self.fixture.client.get('/analysis/searches?category=old&finding=unclassified&exclude_ads=1')
        self.assertEqual(legacy.status_code, 302)
        self.page.goto('http://127.0.0.1' + legacy.headers['Location'])
        expect(self.page.locator('#sa-scatter svg')).to_be_visible()
        self.assertNotIn('category=', self.page.url)
        self.assertNotIn('finding=', self.page.url)
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(2)
        expect(self.page.locator('.sa-products th')).to_have_count(6)
        expect(self.page.locator('.sa-finding-grid>*')).to_have_count(2)
        expect(self.page.get_by_text('产品类型', exact=True)).to_have_count(0)
        self.page.locator('.sa-detail-btn').first.click()
        expect(self.page.locator('#sa-detail-dialog')).to_be_visible()
        expect(self.page.locator('#sa-detail-content')).not_to_contain_text('标题初分')
        self.page.locator('[data-sa-close="sa-detail-dialog"]').click()
        self.page.get_by_role('link', name='店铺分布', exact=True).click()
        expect(self.page.locator('.sa-products th')).to_have_count(5)
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        self.assertEqual(self.errors, [])
