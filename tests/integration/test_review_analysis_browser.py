"""评价分析模型控件的隔离浏览器回归；不调用真实AI或正式数据库。"""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from tests.unit import test_review_analysis as fixtures


@unittest.skipUnless(os.environ.get('ANALYSIS_BROWSER_TESTS') == '1', '显式启用本地分析页浏览器验证')
class ReviewAnalysisBrowserTests(unittest.TestCase):
    def setUp(self):
        """创建脱敏评价、临时应用和独立浏览器；参数：无；返回：无。"""
        self.fixture = fixtures.ReviewAnalysisTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        store = SimpleNamespace(engine=self.fixture.engine, tables=self.fixture.tables)
        for module in ('collection_browser', 'collection_queries'):
            mocked = patch(f'ecommerce.infrastructure.storage.{module}.collection_store', return_value=store)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch('ecommerce.modules.review_analysis.service.create_client')
        self.ai = mocked.start()
        self.addCleanup(mocked.stop)
        self.addCleanup(self.ai.assert_not_called)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1000})
        self.context.route('**/*', self.route)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def route(self, route):
        """仅放行隔离应用的GET请求；参数：浏览器请求；返回：本地响应。"""
        url = urlsplit(route.request.url)
        if url.hostname != '127.0.0.1' or route.request.method != 'GET':
            route.abort()
            return
        if url.path.startswith('/api/'):
            route.fulfill(json={'jobs': []})
            return
        response = self.fixture.client.get(url.path + ('?' + url.query if url.query else ''))
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
        response.close()

    def open_review_dialog(self):
        """打开临时商品的评价确认弹窗；参数：无；返回：弹窗定位器。"""
        self.page.goto('http://127.0.0.1/collections/products/123?module=reviews&reviews_batch=5#review-comments')
        self.page.locator('[data-review-analysis=start]').click()
        dialog = self.page.locator('#review-analysis-dialog')
        expect(dialog.locator('[data-ra-start]')).to_be_visible()
        return dialog

    def verify_repeated_toggle(self, container, native):
        """验证六次开关后仍可检索、选择并重新展开；参数：容器、原生模式；返回：无。"""
        trigger = container.locator('.ss-trigger')
        popup = self.page.locator('#' + trigger.get_attribute('aria-controls'))
        for _ in range(3):
            trigger.click()
            expect(trigger).to_have_attribute('aria-expanded', 'true')
            expect(popup).to_be_visible()
            self.assertFalse(popup.evaluate('(node) => node.hidden'))
            if native:
                self.assertTrue(popup.evaluate('(node) => node.matches(":popover-open")'))
            trigger.click()
            expect(trigger).to_have_attribute('aria-expanded', 'false')
            expect(popup).to_be_hidden()
        trigger.click()
        popup.locator('.ss-input').fill('v4-pro')
        popup.get_by_role('option', name='deepseek-v4-pro', exact=True).click()
        expect(container.locator('select[name=model]')).to_have_value('deepseek-v4-pro')
        expect(trigger).to_be_focused()
        trigger.click()
        expect(popup).to_be_visible()
        popup.locator('.ss-input').press('Escape')
        expect(popup).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_native_model_toggle_on_settings_and_review_dialog(self):
        """原生浮层在设置页和动态评价弹窗中反复开关后仍可选模型。"""
        self.page.goto('http://127.0.0.1/settings/ai')
        self.verify_repeated_toggle(self.page.locator('main'), native=True)
        self.verify_repeated_toggle(self.open_review_dialog(), native=True)

    def test_fallback_model_toggle_on_settings_and_review_dialog(self):
        """无原生Popover的兼容分支也可反复开关和选择模型。"""
        self.context.add_init_script('HTMLElement.prototype.showPopover = undefined;')
        self.page.goto('http://127.0.0.1/settings/ai')
        self.verify_repeated_toggle(self.page.locator('main'), native=False)
        self.verify_repeated_toggle(self.open_review_dialog(), native=False)

    def test_model_light_dismiss_escape_and_dialog_reopen(self):
        """外部点击及Esc收起模型后可重开，关闭弹窗会清理控件并恢复焦点。"""
        dialog = self.open_review_dialog()
        for _ in range(2):
            trigger = dialog.locator('.ss-trigger')
            popup = dialog.locator('.ss-popover')
            trigger.click()
            dialog.locator('h2').click()
            expect(popup).to_be_hidden()
            expect(trigger).to_have_attribute('aria-expanded', 'false')
            trigger.click()
            expect(popup).to_be_visible()
            popup.locator('.ss-input').press('Escape')
            expect(popup).to_be_hidden()
            expect(dialog).to_be_visible()
            trigger.click()
            expect(popup).to_be_visible()
            dialog.get_by_role('button', name='关闭分析弹窗', exact=True).click()
            expect(dialog).to_be_hidden()
            expect(dialog.locator('.ss-popover')).to_have_count(0)
            opener = self.page.locator('[data-review-analysis=start]')
            expect(opener).to_be_focused()
            opener.click()
            expect(dialog.locator('[data-ra-start]')).to_be_visible()
        self.assertEqual(self.errors, [])
