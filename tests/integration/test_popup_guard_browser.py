"""本地HTML全局弹窗回归，不登录、不上传、不提交真实商品。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from ecommerce.platforms.qianniu import product
from ecommerce.platforms.qianniu.product_sku import create_sales_specifications
from ecommerce.platforms.qianniu.product_submit import submit_product_to_warehouse
from ecommerce.platforms.qianniu.popups import PopupGuard
from ecommerce.platforms.qianniu.verification import VerificationGuard, qianniu_operation, page_step, expect, choose_file_dialog
from ecommerce.shared.exceptions import NeedsAttention
from tests.integration.test_sales_specifications_browser import HTML as SKU_HTML
from tests.integration.test_product_submit_browser import HTML as SUBMIT_HTML


BUTTON = '<button id="action" onclick="window.clicks=(window.clicks||0)+1">执行</button>'
UNKNOWN = '<div role="dialog" aria-modal="true" style="position:fixed;inset:0;background:white;z-index:9">未知公告<button>确认</button></div>'
KNOWN = '<div role="dialog" style="position:fixed;inset:0;background:white;z-index:9"><img alt="SKU管理页引导"><button aria-label="关闭" onclick="window.closedNotice=true;this.parentElement.remove()">关闭</button></div>'


@unittest.skipUnless(os.environ.get("QIANNIU_BROWSER_TESTS") == "1", "显式启用本地浏览器测试")
class PopupGuardBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log_dir = Path(self.temp.name)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body='<h1>本地测试</h1>'))
        self.page = self.context.new_page()
        self.page.set_content(BUTTON)

    def guard(self, feedback=None):
        self.popups = PopupGuard(self.context, feedback, self.log_dir)
        return VerificationGuard(self.context, feedback, popup_guard=self.popups)

    def business_action(self, function, module):
        """模拟平台操作边界；参数：本地操作、模块；返回：带真实操作标识的检查入口。"""
        function.__module__ = f'ecommerce.platforms.qianniu.{module}'
        return qianniu_operation(function)

    def test_unknown_mid_action_waits_and_records_before_only_click(self):
        calls = []
        def feedback(message):
            calls.append(message)
            self.assertEqual(self.page.evaluate('window.clicks||0'), 0)
            records = list((self.log_dir/'screenshots').glob('popup-*.json'))
            self.assertEqual(len(records), 1)
            self.assertIn('未知公告', json.loads(records[0].read_text(encoding='utf-8'))['title'])
            self.assertTrue(list((self.log_dir/'screenshots').glob('popup-*.png')))
            self.page.evaluate('document.querySelector("[role=dialog]").remove()')
        @qianniu_operation
        def action():
            self.page.evaluate('(html)=>document.body.insertAdjacentHTML("beforeend",html)', UNKNOWN)
            self.page.locator('#action').click(timeout=3000)
        with self.guard(feedback):
            action()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.page.evaluate('clicks'), 1)

    def test_still_visible_requires_another_explicit_feedback(self):
        self.page.set_content(BUTTON + UNKNOWN)
        calls = []
        def feedback(message):
            calls.append(message)
            self.assertEqual(self.page.evaluate('window.clicks||0'), 0)
            if len(calls) == 2:
                self.page.evaluate('document.querySelector("[role=dialog]").remove()')
        with self.guard(feedback):
            qianniu_operation(lambda: self.page.locator('#action').click())()
        self.assertEqual(len(calls), 2)
        self.assertIn('弹窗尚未处理', calls[1])
        self.assertEqual(len(list((self.log_dir/'screenshots').glob('*.json'))), 1)

    def test_known_announcement_is_closed_even_mid_action(self):
        @qianniu_operation
        def action():
            self.page.evaluate('(html)=>document.body.insertAdjacentHTML("beforeend",html)', KNOWN)
            self.page.locator('#action').click(timeout=3000)
        with self.guard(lambda message: self.fail('已知公告不应请求人工处理')):
            action()
        self.assertTrue(self.page.evaluate('closedNotice'))
        self.assertEqual(self.page.evaluate('clicks'), 1)

    def test_unknown_iframe_dialog_is_checked_before_main_page_click(self):
        self.page.set_content(BUTTON + '<iframe srcdoc="<div role=dialog>框架中的未知弹窗</div>"></iframe>')
        self.page.frames[1].wait_for_load_state()
        calls = []
        def feedback(message):
            calls.append(message)
            self.assertEqual(self.page.evaluate('window.clicks||0'), 0)
            self.page.frames[1].evaluate('document.querySelector("[role=dialog]").remove()')
        with self.guard(feedback):
            qianniu_operation(lambda: self.page.locator('#action').click())()
        self.assertEqual(len(calls), 1)
        self.assertIn('框架中的未知弹窗', calls[0])

    def test_unknown_tab_requires_manual_close(self):
        calls = []
        def feedback(message):
            calls.append(message)
            self.assertEqual(self.page.evaluate('window.clicks||0'), 0)
            popup.close()
        with self.guard(feedback):
            popup = self.context.new_page()
            popup.set_content('<h1>意外窗口</h1>')
            qianniu_operation(lambda: self.page.locator('#action').click())()
        self.assertEqual(len(calls), 1)
        self.assertIn('未知标签页', calls[0])

    def test_native_confirmation_only_uses_explicit_cancellation(self):
        self.page.set_content('<button id="action" onclick="window.accepted=confirm(\'确定删除商品？\')">执行</button>')
        calls = []
        def feedback(message):
            calls.append(message)
            self.assertIn('原生弹窗', message)
            return '取消'
        with self.guard(feedback):
            qianniu_operation(lambda: self.page.locator('#action').click())()
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.page.evaluate('accepted'))

    def test_no_interaction_blocks_unknown_dialog_before_click(self):
        self.page.set_content(BUTTON + UNKNOWN)
        with self.guard():
            with self.assertRaises(NeedsAttention):
                qianniu_operation(lambda: self.page.locator('#action').click())()
        self.assertEqual(self.page.evaluate('window.clicks||0'), 0)

    def test_long_manual_wait_resumes_only_pending_action_and_next_step(self):
        calls = []
        def feedback(message):
            calls.append(message)
            time.sleep(0.3)
            self.page.evaluate('document.querySelector("[role=dialog]").remove()')
        @qianniu_operation
        def action():
            self.page.evaluate('(html)=>document.body.insertAdjacentHTML("beforeend",html)', UNKNOWN)
            page_step(lambda: self.page.locator('#action').click(timeout=100), completed=lambda: self.page.evaluate('window.clicks||0') == 1)
            self.page.evaluate('window.nextStep=true')
        with self.guard(feedback):
            action()
        self.assertEqual(self.page.evaluate('window.clicks||0'), 1)
        self.assertTrue(self.page.evaluate('nextStep'))
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.page.is_closed())

    def test_real_specification_creation_keeps_expected_drawer_usable(self):
        self.page.set_content(SKU_HTML)
        with self.guard(lambda message: self.fail('正常规格面板不应拦截：' + message)):
            self.assertEqual(create_sales_specifications(self.page, ['规格1', '规格2']), ['规格1', '规格2'])
        self.assertEqual(self.page.evaluate('creations'), 1)

    def test_expected_image_and_video_picker_dialogs_are_usable(self):
        for module, source in [('product_images', 'sucai-selector-ng'), ('product_video', 'videoSelector.htm')]:
            with self.subTest(module=module):
                self.context.route(f'**/{source}', lambda route: route.fulfill(content_type='text/html', body=f'<div role="dialog"><input type="file">{BUTTON}</div>'))
                self.page.set_content(f'<div role="dialog"><iframe src="https://qianniu.test/{source}"></iframe></div>')
                frame = self.page.frames[1]
                frame.wait_for_load_state()
                action = self.business_action(lambda: frame.locator('#action').click(timeout=3000), module)
                with self.guard(lambda message: self.fail('正常素材窗口不应拦截：' + message)):
                    action()
                self.assertEqual(frame.evaluate('clicks'), 1)

    def test_unexpected_nested_dialog_inside_picker_still_requires_feedback(self):
        self.page.set_content(f'<div role="dialog"><iframe src="https://qianniu.test/sucai-selector-ng"></iframe>{UNKNOWN}{BUTTON}</div>')
        calls = []
        def feedback(message):
            calls.append(message)
            self.page.evaluate('document.querySelector("[aria-modal=true]").remove()')
        action = self.business_action(lambda: self.page.locator('#action').click(timeout=3000), 'product_images')
        with self.guard(feedback):
            action()
        self.assertEqual(len(calls), 1)
        self.assertIn('未知公告', calls[0])
        self.assertEqual(self.page.evaluate('clicks'), 1)

    def test_captcha_is_handled_before_unknown_popup(self):
        self.page.set_content(BUTTON + UNKNOWN + '<div class="J_MIDDLEWARE_FRAME_WIDGET">验证</div>')
        calls = []
        def feedback(message):
            calls.append(message)
            if '需要人工验证' in message:
                self.page.evaluate('document.querySelector(".J_MIDDLEWARE_FRAME_WIDGET").remove()')
            else:
                self.page.evaluate('document.querySelector("[role=dialog]").remove()')
        with self.guard(feedback):
            qianniu_operation(lambda: self.page.locator('#action').click())()
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith('【需要人工验证】'))
        self.assertTrue(calls[1].startswith('【需要人工处理弹窗】'))

    def test_popup_after_action_does_not_reclick_business_button(self):
        self.page.set_content(BUTTON)
        calls = []
        def feedback(message):
            calls.append(message)
            self.assertEqual(self.page.evaluate('clicks'), 1)
            self.page.evaluate('document.querySelector("[role=dialog]").remove()')
        @qianniu_operation
        def action():
            self.page.locator('#action').click()
            self.page.evaluate('(html)=>document.body.insertAdjacentHTML("beforeend",html)', UNKNOWN)
        with self.guard(feedback):
            action()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.page.evaluate('clicks'), 1)

    def test_unknown_structure_timeout_resumes_pending_action_after_feedback(self):
        self.page.set_content(BUTTON + '<div id="curtain" style="position:fixed;inset:0;background:white;z-index:99">未识别结构</div>')
        calls = []
        def feedback(message):
            calls.append(message)
            self.assertEqual(self.page.evaluate('window.clicks||0'), 0)
            self.page.evaluate('document.querySelector("#curtain").remove()')
        with self.guard(feedback):
            qianniu_operation(lambda: page_step(lambda: self.page.locator('#action').click(timeout=150), completed=lambda: self.page.evaluate('window.clicks||0') == 1))()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.page.evaluate('window.clicks||0'), 1)
        records = list((self.log_dir/'screenshots').glob('*.json'))
        self.assertEqual(json.loads(records[0].read_text(encoding='utf-8'))['kind'], 'interrupted')

    def test_expected_publish_tab_is_not_blocked_as_unknown(self):
        self.page.set_content('<a data-index="2276" target="_blank" href="https://upload.taobao.com/auction/sell.jhtml">发布商品</a>')
        with self.guard(lambda message: self.fail('预期的发布页不应被拦截：' + message)), patch.object(product, 'wait_for_page_ready'):
            result = product.click_publish_product(self.context, self.page)
        self.assertIsNot(result, self.page)
        self.assertEqual(result.url, 'https://upload.taobao.com/auction/sell.jhtml')

    def test_native_confirmation_rejects_ambiguous_continue(self):
        self.page.set_content('<button id="action" onclick="if(confirm(\'确定删除？\')) console.log(\'deleted\')">执行</button>')
        messages = []
        self.page.on('console', lambda message: messages.append(message.text))
        with self.guard(lambda message: '继续'):
            with self.assertRaisesRegex(NeedsAttention, '未收到原生弹窗的明确决定'):
                qianniu_operation(lambda: self.page.locator('#action').click(timeout=3000))()
        self.assertNotIn('deleted', messages)
        self.assertTrue(self.page.is_closed())

    def test_hidden_iframe_dialog_is_not_an_active_popup(self):
        self.page.set_content(BUTTON + '<iframe style="display:none" srcdoc="<div role=dialog>已经隐藏的旧弹窗</div>"></iframe>')
        with self.guard(lambda message: self.fail('隐藏框架不应请求人工处理：' + message)):
            qianniu_operation(lambda: self.page.locator('#action').click())()
        self.assertEqual(self.page.evaluate('clicks'), 1)

    def test_submission_result_in_expected_new_tab_keeps_single_click(self):
        self.context.route('**/sell/success.htm', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body='<h1>商品提交成功</h1><div>商品ID：123456</div>'))
        self.page.set_content(SUBMIT_HTML)
        self.page.evaluate("() => { action=()=>window.open('https://item.upload.taobao.com/sell/success.htm'); }")
        with self.guard(lambda message: self.fail('预期结果页不应请求关闭：' + message)):
            result = submit_product_to_warehouse(self.page, wait_ms=3000)
        self.assertEqual(result.status, 'succeeded', f'{result.message}；页面={[page.url for page in self.context.pages]}')
        self.assertEqual(result.item_id, '123456')
        self.assertIsNot(result.page, self.page)
        self.assertEqual(self.page.evaluate('submitCount'), 1)
        self.assertEqual(len(self.context.pages), 2)

    def test_hidden_business_marker_does_not_allow_unknown_dialog(self):
        self.page.set_content(BUTTON + '<div role="dialog">未知业务提示<iframe hidden src="https://qianniu.test/sucai-selector-ng"></iframe></div>')
        calls = []
        def feedback(message):
            calls.append(message)
            self.page.evaluate('document.querySelector("[role=dialog]").remove()')
        action = self.business_action(lambda: self.page.locator('#action').click(), 'product_images')
        with self.guard(feedback):
            action()
        self.assertEqual(len(calls), 1)
        self.assertIn('未知业务提示', calls[0])

    def test_failed_assertion_resumes_in_place_without_repeating_prior_action(self):
        self.page.set_content(BUTTON + '<div id="pending">等待关闭</div>')
        calls = []
        def feedback(message):
            calls.append(message)
            self.assertEqual(self.page.evaluate('clicks'), 1)
            self.page.evaluate('document.querySelector("#pending").remove()')
        @qianniu_operation
        def action():
            self.page.locator('#action').click()
            expect(self.page.locator('#pending')).to_have_count(0, timeout=100)
            self.page.evaluate('window.nextStep=true')
        with self.guard(feedback):
            action()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.page.evaluate('clicks'), 1)
        self.assertTrue(self.page.evaluate('nextStep'))
        self.assertFalse(self.page.is_closed())

    def test_repeated_unsolved_condition_keeps_waiting_then_continues(self):
        self.page.set_content('<div id="pending">等待关闭</div>')
        calls = []
        def feedback(message):
            calls.append(message)
            if len(calls) == 2:
                self.page.evaluate('document.querySelector("#pending").remove()')
        with self.guard(feedback):
            qianniu_operation(lambda: expect(self.page.locator('#pending')).to_have_count(0, timeout=100))()
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.page.is_closed())

    def test_effective_action_is_not_repeated_after_timeout(self):
        calls = []
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        def already_clicked():
            self.page.locator('#action').click()
            raise PlaywrightTimeoutError('点击生效后等待返回超时')
        with self.guard(lambda message: calls.append(message)):
            qianniu_operation(lambda: page_step(already_clicked, completed=lambda: self.page.evaluate('clicks') == 1))()
        self.assertEqual(self.page.evaluate('clicks'), 1)
        self.assertEqual(calls, [])

    def test_missing_feedback_during_assertion_keeps_step_incomplete(self):
        self.page.set_content('<div id="pending">等待关闭</div>')
        with self.guard():
            with self.assertRaises(NeedsAttention):
                qianniu_operation(lambda: expect(self.page.locator('#pending')).to_have_count(0, timeout=100))()

    def test_file_chooser_survives_long_manual_wait_without_duplicate_selection(self):
        self.page.set_content('<input type="file" onclick="window.fileClicks=(window.fileClicks||0)+1" onchange="window.uploads=(window.uploads||0)+1">')
        calls = []
        def feedback(message):
            calls.append(message)
            time.sleep(0.3)
            self.page.evaluate('document.querySelector("[role=dialog]").remove()')
        @qianniu_operation
        def action():
            self.page.evaluate('(html)=>document.body.insertAdjacentHTML("beforeend",html)', UNKNOWN)
            chooser = choose_file_dialog(self.page, self.page.locator('input'), timeout=100)
            chooser.set_files({'name': 'test.png', 'mimeType': 'image/png', 'buffer': b'local-only'})
        with self.guard(feedback):
            action()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.page.evaluate('fileClicks'), 1)
        self.assertEqual(self.page.evaluate('uploads'), 1)
