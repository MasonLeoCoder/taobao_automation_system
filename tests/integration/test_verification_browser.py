"""本地 HTML 验证守卫测试；运行需设置 QIANNIU_BROWSER_TESTS=1，不访问店铺。"""
import os
import time
import unittest
from playwright.sync_api import sync_playwright
from ecommerce.platforms.qianniu.verification import VerificationGuard, qianniu_operation
from ecommerce.shared.exceptions import NeedsAttention


@unittest.skipUnless(os.environ.get("QIANNIU_BROWSER_TESTS") == "1", "显式启用本地浏览器测试")
class BrowserVerificationTests(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_content('<button onclick="this.textContent=\'done\'">执行</button>')

    def test_iframe_in_new_tab_pauses_before_click(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(self.page.evaluate("document.querySelector('button').textContent"), '执行')
            request.page.frames[1].evaluate("document.querySelector('.J_MIDDLEWARE_FRAME_WIDGET').remove()")
        with VerificationGuard(self.context, handler=handler):
            popup = self.context.new_page()
            popup.set_content('<iframe srcdoc="<div class=J_MIDDLEWARE_FRAME_WIDGET>验证</div>"></iframe>')
            popup.wait_for_timeout(50)
            self.page.locator('button').click()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.page.evaluate("document.querySelector('button').textContent"), 'done')

    def test_long_handler_does_not_replay_timed_out_action(self):
        def handler(request):
            time.sleep(0.3)
            request.page.evaluate("document.querySelector('.J_MIDDLEWARE_FRAME_WIDGET').remove()")
        @qianniu_operation
        def action():
            # 在步骤入口检查之后出现验证，覆盖操作中途的触发路径。
            self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div class=J_MIDDLEWARE_FRAME_WIDGET>验证</div>')")
            self.page.locator('button').click(timeout=100)
        with VerificationGuard(self.context, handler=handler):
            with self.assertRaises(NeedsAttention):
                action()
        self.assertEqual(self.page.evaluate("document.querySelector('button').textContent"), '执行')

    def test_no_callback_blocks_mid_operation_challenge(self):
        clicked = []
        self.page.on("console", lambda message: clicked.append(message.text))
        self.page.evaluate("() => { document.querySelector('button').onclick = () => console.log('clicked'); }")
        @qianniu_operation
        def action():
            self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div class=J_MIDDLEWARE_FRAME_WIDGET>验证</div>')")
            self.page.locator('button').click(timeout=1000)
        with VerificationGuard(self.context):
            with self.assertRaises(NeedsAttention):
                action()
        self.assertTrue(self.page.is_closed())
        self.assertEqual(clicked, [])
