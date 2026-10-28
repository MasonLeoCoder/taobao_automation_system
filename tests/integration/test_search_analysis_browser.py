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
            self.page.locator('#sa-group-select').select_option('received_people:unknown')
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
        self.page.get_by_role('link', name='5–10元，1项商品，筛选明细', exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('.sa-active-filters')).to_contain_text('5–10元')
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
        self.page.locator('#sa-keyword').select_option('手机壳')
        expect(self.page.locator('#sa-snapshot')).to_have_value('range')
        self.page.get_by_role('button', name='应用筛选', exact=True).click()
        expect(self.page.locator('.sa-products tbody tr')).to_have_count(1)
        expect(self.page.locator('#sa-snapshot')).to_have_value('range')
        self.assertEqual(self.errors, [])
