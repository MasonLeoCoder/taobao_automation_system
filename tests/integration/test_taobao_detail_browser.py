"""本地详情页入口测试；不访问真实淘宝。"""
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from urllib.parse import urlsplit
from unittest.mock import patch
from pathlib import Path

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection.detail_service import open_detail_input
from ecommerce.platforms.taobao.detail import open_detail
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import TaskStopped

HOME = '<span class="site-nav-login-info-nick">测试账号</span>'
DETAIL = '<title>待核对商品详情</title><main><h1>粘毛器测试商品</h1></main>'


@unittest.skipUnless(os.environ.get("TAOBAO_BROWSER_TESTS") == "1", "显式启用本地详情页浏览器测试")
class TaobaoDetailBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = replace(load_settings(root=Path(self.temp.name)).for_platform("taobao"), headless=True,
                                taobao_timing={name: (0, 0) for name in DEFAULT_TAOBAO_TIMING})
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.detail_body = DETAIL
        self.detail_status = 200
        self.detail_requests = []

        def route(request):
            is_detail = urlsplit(request.request.url).hostname in {"item.taobao.com", "detail.tmall.com"}
            if is_detail:
                self.detail_requests.append(request.request.url)
            request.fulfill(status=self.detail_status if is_detail else 200,
                            content_type="text/html; charset=utf-8", body=self.detail_body if is_detail else HOME)

        self.context.route("**/*", route)
        self.home = self.context.new_page()
        self.home.goto("https://www.taobao.com/")

    def guard(self, attention=None):
        guard = TaobaoGuard(self.context, self.settings.log_dir, attention, timing=self.settings.taobao_timing)
        guard.login_mode = False
        return guard

    def test_id_opens_fixed_url_once_without_search(self):
        with self.guard():
            result = open_detail(self.home, "1007759256325", 1000)
        self.assertEqual(result.page, self.home)
        self.assertEqual(self.detail_requests, ["https://item.taobao.com/item.htm?id=1007759256325"])
        self.assertEqual(result.target.input_type, "id")

    def test_url_opens_original_query_without_rewriting(self):
        url = "https://item.taobao.com/item.htm?skuId=6001949700618&id=1007759256325&spm=keep"
        with self.guard():
            result = open_detail(self.home, url, 1000)
        self.assertEqual(self.detail_requests, [url])
        self.assertEqual(result.target.input_type, "url")

    def test_verification_requires_feedback_and_does_not_navigate_again(self):
        self.detail_body += '<div class="J_MIDDLEWARE_FRAME_WIDGET">请拖动下方滑块完成验证</div>'
        messages = []

        def attention(message):
            messages.append(message)
            self.home.locator('.J_MIDDLEWARE_FRAME_WIDGET').evaluate('e=>e.remove()')
            return "继续"

        with self.guard(attention):
            open_detail(self.home, "1007759256325", 1000)
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(self.detail_requests), 1)

    def test_wrong_redirected_id_stops_and_never_reopens(self):
        self.detail_body += "<script>history.replaceState(null,'','?id=1050158363561')</script>"
        with self.guard(lambda message: "停止"):
            with self.assertRaises(TaskStopped):
                open_detail(self.home, "1007759256325", 100)
        self.assertEqual(len(self.detail_requests), 1)

    def test_rate_limit_stops_before_entry_is_reported_ready(self):
        self.detail_status = 429
        with self.guard(lambda message: "停止"):
            with self.assertRaises(TaskStopped):
                open_detail(self.home, "1007759256325", 1000)
        self.assertEqual(len(self.detail_requests), 1)

    def test_independent_service_records_entry_and_pending_dom_status(self):
        from ecommerce.platforms.taobao import session

        @contextmanager
        def browser(settings):
            yield self.context, self.home

        with patch.object(session, "base_browser_session", browser):
            open_detail_input(self.settings, "1007759256325", wait=False)
        paths = list((self.settings.workspace / "output/raw/taobao_detail_open").glob('*.json'))
        self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_text(encoding='utf-8'))
        self.assertEqual(record['input_type'], 'id')
        self.assertEqual(record['status'], 'opened_pending_dom_review')
        self.assertFalse(record['product_fields_collected'])
        self.assertTrue(Path(record['screenshot']).exists())
        self.assertEqual(len(self.detail_requests), 1)


if __name__ == "__main__":
    unittest.main()
