"""真实浏览器、本地模拟响应；TAOBAO_BROWSER_TESTS=1 启用，不访问淘宝。"""
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.cli.interaction import prompt_taobao_attention
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped
from ecommerce.shared.config import load_settings

HOME = "https://www.taobao.com/"
BODY = '<span class="site-nav-login-info-nick">测试账号</span><button onclick="window.clicks=(window.clicks||0)+1">执行</button>'


@unittest.skipUnless(os.environ.get("TAOBAO_BROWSER_TESTS") == "1", "显式启用淘宝本地浏览器测试")
class TaobaoBrowserGuardTests(unittest.TestCase):
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
        self.log_dir = Path(self.temp.name) / "logs"
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.responses = {}
        def route(request):
            path = urlsplit(request.request.url).path
            status, headers, body = self.responses.get(path, (200, {}, BODY))
            request.fulfill(status=status, headers=headers, content_type="text/html", body=body)
        self.context.route("**/*", route)
        self.page = self.context.new_page()
        self.page.goto(HOME)

    def guard(self, callback=None):
        return TaobaoGuard(self.context, self.log_dir, callback)

    def test_popup_removal_without_continue_does_not_release_action(self):
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div role=dialog>人工处理</div>')")
        calls = []
        def attention(message):
            calls.append(message)
            self.assertIsNone(self.page.evaluate("window.clicks"))
            if len(calls) == 1:
                self.page.locator('[role="dialog"]').evaluate("e=>e.remove()")
                return ""
            return "继续"
        with self.guard(attention) as guard:
            guard.perform("点击", lambda: self.page.get_by_role("button").click())
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.page.evaluate("window.clicks"), 1)
            events = [json.loads(line) for line in guard.event_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(event.get("screenshot") and Path(event["screenshot"]).is_file() for event in events))

    def test_still_visible_verification_requires_second_continue(self):
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div class=J_MIDDLEWARE_FRAME_WIDGET>滑块</div>')")
        messages = []
        def attention(message):
            messages.append(message)
            if len(messages) == 2:
                self.page.locator('.J_MIDDLEWARE_FRAME_WIDGET').evaluate("e=>e.remove()")
            return "继续"
        with self.guard(attention) as guard:
            guard.perform("读取", lambda: self.page.get_by_role("button").click())
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[1].startswith("【验证尚未通过】"))
        self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_visible_iframe_verification_and_hidden_iframe(self):
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend', `<iframe style='display:none' srcdoc='<div class=J_MIDDLEWARE_FRAME_WIDGET>验证</div>'></iframe>`)")
        with self.guard() as guard:
            guard.checkpoint()
            self.page.locator('iframe').evaluate("e=>e.style.display='block'")
            with self.assertRaises(NeedsAttention):
                guard.perform("读取", lambda: self.fail("验证未处理不能执行"))

    def test_wait_detects_delayed_challenge_and_excludes_manual_time(self):
        self.page.evaluate("setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<div class=J_MIDDLEWARE_FRAME_WIDGET>验证</div>'),20)")
        def attention(message):
            time.sleep(.2)
            self.page.locator('.J_MIDDLEWARE_FRAME_WIDGET').evaluate("e=>e.remove()")
            return "继续"
        with self.guard(attention) as guard:
            started = time.monotonic()
            guard.wait(400)
            self.assertGreaterEqual(guard.paused_seconds, .2)
            self.assertGreaterEqual(time.monotonic() - started, .59)

    def test_unexpected_tab_must_be_closed_before_resume(self):
        messages = []
        extra = None
        def attention(message):
            messages.append(message)
            extra.close()
            return "继续"
        with self.guard(attention) as guard:
            guard.login_mode = False
            extra = self.context.new_page()
            extra.goto(HOME + "other")
            guard.checkpoint()
        self.assertEqual(len(messages), 1)

    def test_native_confirm_requires_decision_then_continue(self):
        messages = []
        def attention(message):
            messages.append(message)
            return "取消" if message.startswith("【需要人工决定原生弹窗】") else "继续"
        with self.guard(attention) as guard:
            result = guard.perform("原生确认", lambda: self.page.evaluate("confirm('是否继续页面动作？')"))
        self.assertFalse(result)
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[1].startswith("【验证尚未通过】"))

    def test_response_cooldown_needs_fresh_feedback_after_expiry(self):
        self.responses["/limited"] = (429, {"Retry-After": "1"}, BODY)
        messages = []
        def attention(message):
            messages.append(message)
            if len(messages) == 2:
                self.page.wait_for_timeout(1050)
            return "继续"
        with self.guard(attention) as guard:
            response = guard.perform("访问受限页面", lambda: self.page.goto(HOME + "limited"))
            self.assertEqual(response.status, 429)
            self.assertEqual(len(messages), 2)
            self.assertFalse(guard._pending)

    def test_image_failure_is_ignored_but_registered_product_response_pauses(self):
        self.responses["/api/list"] = (403, {}, "denied")
        self.responses["/image.png"] = (429, {}, "image failure")
        messages = []
        with self.guard(lambda message: messages.append(message) or "继续") as guard:
            self.page.evaluate("fetch('/image.png').then(r=>r.text())")
            guard.checkpoint()
            self.assertFalse(messages)
            guard.watch_responses(lambda request: urlsplit(request.url).path == "/api/list")
            self.page.evaluate("fetch('/api/list').then(r=>r.text())")
            guard.checkpoint()
            self.assertEqual(len(messages), 1)

    def test_login_expiry_and_resume_validation(self):
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div class=site-nav-login><a href=https://login.taobao.com/>亲，请登录</a></div>')")
        messages = []
        def attention(message):
            messages.append(message)
            if self.page.locator('.site-nav-login').count():
                self.page.locator('.site-nav-login').evaluate("e=>e.remove()")
            return "继续"
        with self.guard(attention) as guard:
            guard.login_mode = False
            guard.resume_check = lambda: len(messages) > 1
            guard.checkpoint()
        self.assertEqual(len(messages), 2)

    def test_pause_stop_and_closed_window_do_not_run_next_action(self):
        with self.guard(lambda message: "停止") as guard:
            guard.control_path.parent.mkdir(parents=True)
            guard.control_path.write_text('{"action":"pause"}')
            with self.assertRaises(TaskStopped):
                guard.perform("执行", lambda: self.fail("停止后不能继续"))
        self.context.close()
        with self.guard() as guard:
            with self.assertRaises(TaskStopped):
                guard.checkpoint()

    def test_real_feedback_loop_keeps_browser_events_alive_until_continue(self):
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div role=dialog>待人工</div>'); setTimeout(()=>document.querySelector('[role=dialog]').remove(),100)")
        with patch("ecommerce.cli.interaction._play_attention_sound"), self.guard(prompt_taobao_attention) as guard:
            guard._feedback_thread = object()
            # 只由后台线程投递反馈，不从其他线程访问浏览器。
            timer = threading.Timer(.3, lambda: guard._feedback_queue.put("继续"))
            timer.start()
            self.addCleanup(timer.join)
            started = time.monotonic()
            guard.perform("人工反馈", lambda: self.page.get_by_role('button').click())
            self.assertGreaterEqual(time.monotonic() - started, .29)
            self.assertEqual(self.page.evaluate("window.clicks"), 1)

    def test_account_check_installs_guard_and_saves_only_confirmed_session(self):
        from ecommerce.modules.account import taobao_service
        from ecommerce.platforms.taobao import session
        from ecommerce.platforms.taobao.verification import active_guard
        settings = replace(load_settings(root=Path(self.temp.name)).for_platform("taobao"), headless=True)
        @contextmanager
        def browser(settings):
            yield self.context, self.page
        with patch.object(session, "base_browser_session", browser):
            self.assertIn("登录状态有效", taobao_service.check(settings))
        self.assertTrue(settings.session_path.is_file())
        self.assertIsNone(active_guard())


if __name__ == "__main__":
    unittest.main()
