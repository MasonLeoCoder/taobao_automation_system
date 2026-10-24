"""本机 Chrome 连接只访问本次进程，保留平台和配置边界。"""
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from ecommerce.infrastructure.browser import native_chrome
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention


class NativeChromeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = replace(load_settings(root=self.root).for_platform("taobao"),
                                taobao_browser_channel="chrome", taobao_browser_mode="native_cdp")

    def test_native_configuration_needs_visible_system_chrome(self):
        config = self.root / "native.toml"
        for text in ('[taobao]\nbrowser_mode="native_cdp"',
                     '[taobao]\nbrowser_mode="native_cdp"\nbrowser_channel="msedge"',
                     '[browser]\nheadless=true\n[taobao]\nbrowser_mode="native_cdp"\nbrowser_channel="chrome"',
                     '[taobao]\nbrowser_mode="unknown"'):
            config.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                load_settings(str(config), root=self.root)
        config.write_text('[taobao]\nbrowser_mode="native_cdp"\nbrowser_channel="chrome"', encoding="utf-8")
        selected = load_settings(str(config), root=self.root)
        self.assertEqual(selected.for_platform("taobao").taobao_browser_mode, "native_cdp")
        self.assertEqual(selected.profile_path, self.root / "workspace/storage/browser_profiles/qianniu")

    def test_wrong_endpoint_is_rejected_before_connecting(self):
        opener = MagicMock()
        opener.open.return_value = io.BytesIO(json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:9001/devtools/browser/x"}).encode())
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(native_chrome, "build_opener", return_value=opener):
            with self.assertRaisesRegex(NeedsAttention, "预期端口不一致"):
                native_chrome.wait_for_endpoint(process, "http://127.0.0.1:9000", 1000)
        opener.open.assert_called_once()

    def run_context(self, owner_pid, action):
        process = MagicMock(pid=123)
        process.poll.return_value = None
        context = MagicMock()
        browser = MagicMock()
        browser.contexts = [context]
        session = browser.new_browser_cdp_session.return_value
        session.send.side_effect = lambda method: {"processInfo": [{"type": "browser", "id": owner_pid}]} if method == "SystemInfo.getProcessInfo" else None
        with patch.object(native_chrome, "find_chrome", return_value=Path("chrome.exe")), \
                patch.object(native_chrome, "available_local_port", return_value=9000), \
                patch.object(native_chrome, "wait_for_endpoint"), \
                patch.object(native_chrome.subprocess, "Popen", return_value=process) as launch, \
                patch.object(native_chrome, "load_session") as restore, \
                patch("playwright.sync_api.sync_playwright") as factory:
            runtime = factory.return_value.__enter__.return_value
            runtime.chromium.connect_over_cdp.return_value = browser
            action(process, context, session, launch, restore, runtime)

    def test_owned_browser_uses_existing_context_and_closes_normally(self):
        def action(process, context, session, launch, restore, runtime):
            with native_chrome.browser_session(self.settings, restore_state=False) as (result, page):
                self.assertIs(result, context)
                self.assertIs(page, context.pages[0])
            restore.assert_not_called()
            runtime.chromium.connect_over_cdp.assert_called_once_with("http://127.0.0.1:9000", timeout=60000, no_defaults=True)
            arguments = launch.call_args.args[0]
            self.assertIn(f"--user-data-dir={self.settings.profile_path}", arguments)
            self.assertNotIn("--enable-automation", arguments)
            self.assertNotIn("--no-sandbox", arguments)
            self.assertIn("Browser.close", [call.args[0] for call in session.send.call_args_list])
            process.terminate.assert_not_called()
        self.run_context(123, action)

    def test_unowned_connection_never_restores_state_or_closes_that_browser(self):
        def action(process, context, session, launch, restore, runtime):
            with self.assertRaisesRegex(NeedsAttention, "无法确认连接"):
                with native_chrome.browser_session(self.settings):
                    self.fail("连接身份未确认，不能读取页面")
            restore.assert_not_called()
            self.assertEqual([call.args[0] for call in session.send.call_args_list], ["SystemInfo.getProcessInfo"])
            context.set_default_timeout.assert_not_called()
            process.terminate.assert_called_once()
        self.run_context(456, action)

    def test_taobao_routes_candidate_mode_and_keeps_fresh_profile(self):
        from ecommerce.platforms.taobao import session
        (self.settings.profile_path / "Default").mkdir(parents=True)
        with patch.object(native_chrome, "browser_session") as factory, \
                patch.object(session, "base_browser_session") as legacy, patch.object(session, "TaobaoGuard"), \
                patch.object(session, "restore_missing_session_cookies") as restore_missing:
            factory.return_value.__enter__.return_value = (MagicMock(), MagicMock())
            with session.browser_session(self.settings):
                pass
            factory.assert_called_once_with(self.settings, restore_state=False)
            restore_missing.assert_called_once_with(factory.return_value.__enter__.return_value[0], self.settings)
            legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
