"""系统 Chrome、本地页面候选路径检查；不访问淘宝、不使用真实资料。"""
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ecommerce.infrastructure.browser.native_chrome import browser_session, find_chrome
from ecommerce.infrastructure.browser.session import save_session
from ecommerce.platforms.taobao.session import browser_session as guarded_browser_session
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.config import load_settings


@unittest.skipUnless(os.environ.get("TAOBAO_SYSTEM_CHROME_TESTS") == "1", "显式启用系统 Chrome 本地测试")
class NativeChromeBrowserTests(unittest.TestCase):
    def test_guarded_session_restores_session_cookie_without_rolling_back_profile(self):
        """关闭重开后补回会话 Cookie，同时保留资料中较新的持久 Cookie。"""
        try:
            find_chrome()
        except ValueError:
            self.skipTest("本机没有系统 Chrome")
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(load_settings(root=Path(folder)).for_platform("taobao"),
                               taobao_browser_channel="chrome", taobao_browser_mode="native_cdp")
            with guarded_browser_session(settings) as (context, page):
                context.add_cookies([
                    {"name": "session-login", "value": "confirmed", "domain": ".local.test", "path": "/"},
                    {"name": "persistent", "value": "old-export", "domain": ".local.test", "path": "/", "expires": 2000000000},
                ])
                save_session(context, settings)
                context.add_cookies([
                    {"name": "persistent", "value": "fresh-profile", "domain": ".local.test", "path": "/", "expires": 2000000000},
                ])
            with guarded_browser_session(settings) as (context, page):
                cookies = {item["name"]: item for item in context.cookies()}
                self.assertIn("session-login", cookies)
                self.assertEqual(cookies["session-login"]["value"], "confirmed")
                self.assertEqual(cookies["session-login"]["expires"], -1)
                self.assertEqual(cookies["persistent"]["value"], "fresh-profile")

    def test_normal_connection_can_read_fill_click_once_and_preserve_profile(self):
        try:
            find_chrome()
        except ValueError:
            self.skipTest("本机没有系统 Chrome")
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(load_settings(root=Path(folder)).for_platform("taobao"),
                               taobao_browser_channel="chrome", taobao_browser_mode="native_cdp")
            with browser_session(settings, restore_state=False) as (context, page):
                page.goto("about:blank")
                page.set_content('<input id="keyword"><button onclick="window.submits=(window.submits||0)+1">Search</button>')
                self.assertIs(page.evaluate("navigator.webdriver"), False)
                page.locator("#keyword").fill("粘毛器")
                page.get_by_role("button").click()
                self.assertEqual(page.locator("#keyword").input_value(), "粘毛器")
                self.assertEqual(page.evaluate("window.submits"), 1)
                context.add_cookies([{"name": "local-probe", "value": "fresh", "domain": ".local.test", "path": "/", "expires": 2000000000}])
                self.assertIn("fresh", [cookie["value"] for cookie in context.cookies() if cookie["name"] == "local-probe"])
            with browser_session(settings, restore_state=False) as (context, page):
                self.assertIn("fresh", [cookie["value"] for cookie in context.cookies() if cookie["name"] == "local-probe"])
                self.assertIs(page.evaluate("navigator.webdriver"), False)

    def test_candidate_keeps_manual_verification_barrier_before_click(self):
        try:
            find_chrome()
        except ValueError:
            self.skipTest("本机没有系统 Chrome")
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(load_settings(root=Path(folder)).for_platform("taobao"),
                               taobao_browser_channel="chrome", taobao_browser_mode="native_cdp")
            messages = []
            target = None

            def attention(message):
                messages.append(message)
                self.assertIsNone(target.evaluate("window.submits"))
                target.locator('[role="dialog"]').evaluate("element=>element.remove()")
                return "继续"

            with guarded_browser_session(settings, on_attention=attention) as (context, page):
                target = page
                page.goto("about:blank")
                page.set_content('<div role="dialog">待人工处理</div><button onclick="window.submits=(window.submits||0)+1">Search</button>')
                guard = active_guard()
                self.assertIsNotNone(guard)
                guard.perform("本地单次提交", lambda: page.get_by_role("button").click())
                self.assertEqual(len(messages), 1)
                self.assertEqual(page.evaluate("window.submits"), 1)


if __name__ == "__main__":
    unittest.main()
