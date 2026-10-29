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

    def test_preview_auto_budget_and_insufficient_manual_limit(self):
        """发送预览可读，默认预算不需填写，手动不足在提交前提示且不发送请求。"""
        dialog = self.open_review_dialog()
        expect(dialog).to_contain_text('共 2 次请求')
        expect(dialog.locator('[name=max_tokens]')).to_be_disabled()
        preview = dialog.locator('details').filter(has=self.page.locator('summary', has_text='查看发送内容')).first
        preview.locator('summary').first.click()
        group = preview.locator('details').first
        group.locator('summary').click()
        expect(group.locator('pre')).to_contain_text('刚买很好用')
        expect(group.locator('pre')).to_contain_text('用了几天粘不住')
        for width in (390, 1440):
            self.page.set_viewport_size({'width':width,'height':1000})
            bounds = group.locator('pre').evaluate('(node) => ({height:node.clientHeight,width:node.getBoundingClientRect().width,parent:node.parentElement.getBoundingClientRect().width})')
            self.assertLessEqual(bounds['height'], 360)
            self.assertLessEqual(bounds['width'], bounds['parent'] + 1)
            self.assertTrue(self.page.evaluate('() => document.documentElement.scrollWidth <= innerWidth + 1'))
        self.assertNotIn('不可发送的昵称', preview.inner_text())
        self.assertNotIn('sk-test-only', preview.inner_text())
        advanced = dialog.locator('details').filter(has=self.page.locator('summary', has_text='高级设置')).first
        advanced.locator('summary').first.click()
        advanced.locator('[name=manual_budget]').check()
        tokens = advanced.locator('[name=max_tokens]')
        expect(tokens).to_be_enabled()
        tokens.fill('1')
        expect(advanced.locator('[data-ra-budget-feedback]')).to_contain_text('还差')
        self.assertFalse(tokens.evaluate('(node) => node.validity.valid'))
        advanced.locator('[name=manual_budget]').uncheck()
        expect(tokens).to_be_disabled()
        requests = []
        self.page.route('**/api/analysis/reviews/products/123', lambda route: (
            requests.append(route.request.post_data_json), route.fulfill(json={'url':'/analysis/reviews/products/123?batch=5'})))
        dialog.locator('[name=allow_paid]').check()
        with self.page.expect_navigation():
            dialog.get_by_role('button', name='开始分析', exact=True).click()
        self.assertEqual(len(requests), 1)
        self.assertIsNone(requests[0]['max_tokens'])
        self.assertIsNone(requests[0]['max_calls'])
        self.assertTrue(requests[0]['plan_hash'])
        self.assertEqual(self.errors, [])

    def test_partial_result_explicit_budget_increase(self):
        """结果页明确追加额度后才提交新上限，不重新发送已保存评价。"""
        from ecommerce.infrastructure.storage import analysis_store as repository
        with patch('ecommerce.modules.review_analysis.service.create_client'):
            task = self.fixture.start()
        parameters = dict(repository.read(self.fixture.settings, task)['task']['parameters'])
        with self.fixture.engine.begin() as conn:
            conn.execute(self.fixture.tables['analysis_tasks'].update().values(parameters={**parameters,'max_calls':1}))
        self.fixture.run_mock(task, [self.fixture.response(self.fixture.classified())])
        self.page.goto('http://127.0.0.1/analysis/reviews/tasks/' + task)
        expect(self.page.locator('#ra-progress')).to_contain_text('总报告未完成')
        form = self.page.locator('[data-ra-resume]')
        expect(form.locator('[name=max_calls]')).to_be_disabled()
        form.locator('[name=increase_budget]').check()
        expect(form.locator('[name=max_calls]')).to_be_enabled()
        expect(form.locator('[name=max_calls]')).to_have_value('2')
        requests = []
        self.page.route('**/api/analysis/reviews/tasks/' + task + '/resume', lambda route: (
            requests.append(route.request.post_data_json), route.fulfill(json={'id':task})))
        form.locator('[name=allow_paid]').check()
        with self.page.expect_navigation():
            form.get_by_role('button', name='继续未完成部分', exact=True).click()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['max_calls'], 2)
        self.assertGreaterEqual(requests[0]['max_tokens'], parameters['max_tokens'])
        self.assertEqual(len(repository.read(self.fixture.settings, task)['details']), 2)
        self.assertEqual(self.errors, [])
