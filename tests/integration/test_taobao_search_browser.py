"""关键词搜索真实浏览器测试，所有网页与响应都在本地模拟。"""
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from ecommerce.platforms.taobao.search import canonical_search_url, inspect_search, matches_search_url, open_search
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import DEFAULT_TAOBAO_TIMING, load_settings
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped

HOME = '<span class="site-nav-login-info-nick">测试账号</span><form id="J_TSearchForm" action="https://s.taobao.com/search" target="_blank"><input id="q" name="q"><button class="btn-search">搜索</button></form>'
RESULTS = '<input name="q" id="q"><a href="https://item.taobao.com/item.htm?id=123456">商品一</a><a href="https://item.taobao.com/item.htm?id=123456">同商品图片</a><a href="https://detail.tmall.com/item.htm?id=234567">商品二</a>'


@unittest.skipUnless(os.environ.get("TAOBAO_BROWSER_TESTS") == "1", "显式启用淘宝本地浏览器测试")
class TaobaoSearchBrowserTests(unittest.TestCase):
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
        self.search_body = RESULTS
        self.search_status = 200
        self.search_requests = 0
        def route(request):
            url = urlsplit(request.request.url)
            if url.hostname == "s.taobao.com":
                self.search_requests += 1
                keyword = parse_qs(url.query).get("q", [""])[0]
                body = self.search_body + '<script>document.querySelector("input[name=q]").value=' + json.dumps(keyword) + '</script>'
            else:
                body = HOME
            request.fulfill(status=self.search_status if url.hostname == "s.taobao.com" else 200,
                            content_type="text/html; charset=utf-8", body=body)
        self.context.route("**/*", route)
        self.home = self.context.new_page()
        self.home.goto("https://www.taobao.com/")

    def guard(self, callback=None):
        guard = TaobaoGuard(self.context, self.settings.log_dir, callback, timing=self.settings.taobao_timing)
        guard.login_mode = False
        return guard

    def test_home_form_search_opens_expected_tab_with_exact_keyword(self):
        keyword = "粘毛器 & + #"
        with self.guard() as guard:
            result = open_search(self.context, self.home, keyword, 3000)
            self.assertNotEqual(result.page, self.home)
            self.assertTrue(matches_search_url(result.page, keyword))
            self.assertEqual((result.status, result.visible_items), ("results", 2))
            events = [json.loads(line) for line in guard.event_path.read_text(encoding="utf-8").splitlines()]
            before = next(index for index, event in enumerate(events) if event.get("action") == "before_search")
            submits = [index for index, event in enumerate(events) if event["event"] == "operation_started" and event["step"] == "提交淘宝关键词搜索"]
            after = next(index for index, event in enumerate(events) if event.get("action") == "search")
            self.assertEqual(len(submits), 1)
            self.assertLess(before, submits[0])
            self.assertLess(submits[0], after)
            requests = [event for event in events if event["event"] == "document_requested" and event["location"] == "https://s.taobao.com/search"]
            self.assertEqual(len(requests), 1)
        self.assertEqual(self.search_requests, 1)

    def test_same_tab_form_submission_is_supported(self):
        self.home.locator('form').evaluate("e=>e.target='_self'")
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 3000)
            self.assertEqual(result.page, self.home)
        self.assertEqual(self.search_requests, 1)

    def test_old_matching_tab_cannot_replace_the_new_search(self):
        old = self.context.new_page()
        old.goto(canonical_search_url("粘毛器"))
        with self.guard():
            result = open_search(self.context, self.home, "粘毛器", 3000)
            self.assertNotEqual(result.page, old)
        self.assertEqual(self.search_requests, 2)

    def test_blank_result_is_not_reported_as_empty_or_researched(self):
        self.search_body = '<input name="q">'
        with self.guard(lambda message: "停止"):
            with self.assertRaises(TaskStopped):
                open_search(self.context, self.home, "粘毛器", 200)
        self.assertEqual(self.search_requests, 1)

    def test_verification_and_multiple_feedback_do_not_repeat_submission(self):
        self.search_body = RESULTS + '<div class="J_MIDDLEWARE_FRAME_WIDGET">请拖动下方滑块完成验证</div>'
        messages = []
        def attention(message):
            messages.append(message)
            if len(messages) == 2:
                page = next(page for page in self.context.pages if matches_search_url(page, "粘毛器"))
                page.locator('.J_MIDDLEWARE_FRAME_WIDGET').evaluate('e=>e.remove()')
            return "继续"
        with self.guard(attention) as guard:
            result = open_search(self.context, self.home, "粘毛器", 3000)
            self.assertEqual(result.status, "results")
            events = [json.loads(line) for line in guard.event_path.read_text(encoding="utf-8").splitlines()]
            submits = [event for event in events if event["event"] == "operation_started" and event["step"] == "提交淘宝关键词搜索"]
            self.assertEqual(len(submits), 1)
        self.assertEqual(len(messages), 2)
        self.assertEqual(self.search_requests, 1)

    def test_changing_keyword_during_pre_submit_wait_requires_review(self):
        calls = []
        def attention(message):
            calls.append(message)
            self.assertEqual(self.search_requests, 0)
            self.home.locator('#q').fill('粘毛器')
            return "继续"
        with self.guard(attention) as guard:
            original_pause = guard.pause_between
            def pause(action):
                if action == "before_search":
                    self.home.locator('#q').fill('其他关键词')
                original_pause(action)
            guard.pause_between = pause
            result = open_search(self.context, self.home, "粘毛器", 3000)
            self.assertEqual(result.status, "results")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.search_requests, 1)

    def test_first_response_of_new_search_tab_cannot_skip_rate_limit_guard(self):
        self.search_status = 429
        messages = []
        with self.guard(lambda message: messages.append(message) or "停止"):
            with self.assertRaises(TaskStopped):
                open_search(self.context, self.home, "粘毛器", 3000)
        self.assertEqual(self.search_requests, 1)
        self.assertTrue(any("HTTP 429" in message for message in messages))

    def test_explicit_empty_result_and_hidden_products(self):
        self.search_body = '<input name="q"><p>抱歉，没有找到相关宝贝</p><a style="display:none" href="https://item.taobao.com/item.htm?id=123">隐藏模板</a>'
        with self.guard():
            result = open_search(self.context, self.home, "不存在的商品", 3000)
            self.assertEqual((result.status, result.visible_items), ("empty", 0))

    def test_wrong_input_keyword_is_not_accepted(self):
        page = self.context.new_page()
        page.goto(canonical_search_url("粘毛器"))
        page.locator('input').fill('其他关键词')
        self.assertIsNone(inspect_search(page, "粘毛器"))

    def test_offscreen_and_transparent_links_do_not_count_as_visible_results(self):
        page = self.context.new_page()
        page.goto(canonical_search_url("粘毛器"))
        page.locator('a').evaluate_all("els=>els.forEach(e=>e.style.opacity='0')")
        page.evaluate("document.body.insertAdjacentHTML('beforeend','<a style=\"position:absolute;top:100000px\" href=\"https://item.taobao.com/item.htm?id=345678\">屏外商品</a>')")
        self.assertIsNone(inspect_search(page, "粘毛器"))

    def test_expected_page_rule_does_not_allow_wrong_keyword(self):
        extra = None
        def attention(message):
            extra.close()
            return "继续"
        with self.guard(attention) as guard:
            with guard.expect_new_page(lambda page: matches_search_url(page, "粘毛器")):
                extra = self.context.new_page()
                extra.goto(canonical_search_url("wrong"))
                guard.checkpoint()
            self.assertTrue(extra.is_closed())

    def test_search_service_keeps_taobao_session_and_writes_safe_summary(self):
        from ecommerce.modules.collection import service
        from ecommerce.platforms.taobao import session
        @contextmanager
        def browser(settings):
            yield self.context, self.home
        with patch.object(session, "base_browser_session", browser):
            message = service.search_keyword(self.settings, "粘毛器", wait=False)
        files = list((self.settings.workspace / "output/raw/taobao_search").glob('*.json'))
        self.assertEqual(len(files), 1)
        summary = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual((summary["keyword"], summary["status"], summary["visible_items"]), ("粘毛器", "results", 2))
        self.assertEqual(summary["source_url"], canonical_search_url("粘毛器"))
        self.assertTrue(self.settings.session_path.exists())
        self.assertIn("首屏商品列表已加载", message)

    def test_fresh_profile_cookie_is_not_overwritten_by_older_export(self):
        from ecommerce.infrastructure.browser.session import browser_session as base_session
        from ecommerce.platforms.taobao.session import browser_session
        @contextmanager
        def existing_playwright():
            yield self.playwright
        with patch("playwright.sync_api.sync_playwright", existing_playwright):
            with base_session(self.settings, restore_state=False) as (context, page):
                context.add_cookies([{"name": "test-session", "value": "fresh", "domain": ".taobao.com", "path": "/", "expires": 2000000000}])
            self.assertTrue((self.settings.profile_path / "Default").is_dir())
            self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings.session_path.write_text(json.dumps({"cookies": [{"name": "test-session", "value": "old", "domain": ".taobao.com", "path": "/", "expires": 2000000000}], "origins": []}), encoding="utf-8")
            with browser_session(self.settings) as (context, page):
                values = [cookie["value"] for cookie in context.cookies() if cookie["name"] == "test-session"]
                self.assertEqual(values, ["fresh"])


if __name__ == "__main__":
    unittest.main()
