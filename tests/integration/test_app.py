"""验证页面、命令和单浏览器业务编排。"""
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.web.app import create_app


class AppTests(unittest.TestCase):
    """临时数据与模拟页面集成测试。"""

    def setUp(self):
        """建立临时应用；参数：无；返回：无。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = load_settings(root=Path(self.temp.name))
        self.app = create_app(self.settings)
        self.client = self.app.test_client()

    def test_pages_and_empty_state(self):
        """所有导航均可访问且空状态真实；参数：无；返回：无。"""
        for path in ("/", "/tasks", "/modules/account", "/modules/product", "/modules/collection", "/modules/selection", "/modules/reports", "/modules/analysis", "/health", "/static/app.css"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200, path)
        self.assertEqual(self.client.get("/modules/missing").status_code, 404)
        self.assertIn("还没有执行记录", self.client.get("/tasks").get_data(as_text=True))
        self.assertEqual(self.client.get("/api/status").json["total"], 0)
        self.assertEqual(self.client.post("/api/status").status_code, 405)

    def test_real_records_and_html_escaping(self):
        """任务页面来自数据库并转义文本；参数：无；返回：无。"""
        run_task(self.settings, "<script>alert(1)</script>", lambda: "<b>done</b>")
        data = self.client.get("/api/status").json
        self.assertEqual(data["counts"]["succeeded"], 1)
        page = self.client.get("/tasks").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_cli_from_another_directory(self):
        """从外部目录运行及未实现命令退出状态；参数：无；返回：无。"""
        env = dict(os.environ, ECOMMERCE_ROOT=self.temp.name, PYTHONUTF8="1")
        for args, expected in [(["--help"], 0), (["status"], 0), (["tasks"], 0), (["download"], 2), (["product", "draft"], 2), (["web", "--port", "0"], 1), (["--config", "missing.toml", "status"], 1)]:
            result = subprocess.run([sys.executable, str(ROOT / "main.py"), *args], cwd=self.temp.name, env=env, capture_output=True)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_publish_reuses_browser_and_keeps_order(self):
        """登录与发布复用一个上下文且按序执行；参数：无；返回：无。"""
        from ecommerce.modules.product import service
        context, page = MagicMock(), MagicMock()
        calls = []

        @contextmanager
        def session(settings):
            """模拟一次浏览器生命周期；参数：配置；返回：上下文和页面。"""
            calls.append("open")
            yield context, page
            calls.append("close")

        with patch.object(service, "browser_session", session), patch.object(service, "authenticate", side_effect=lambda *a: calls.append("login")), patch.object(service, "click_product_nav", side_effect=lambda *a: calls.append("product") or page), patch.object(service, "click_publish_product", side_effect=lambda *a: calls.append("publish") or page), patch.object(service, "click_start_button", side_effect=lambda *a: calls.append("start")):
            service.open_publish(self.settings, True, lambda: calls.append("ready"))
        self.assertEqual(calls, ["open", "login", "product", "publish", "start", "ready", "close"])

    def test_login_failure_never_saves_state(self):
        """失效会话不会被标记为成功；参数：无；返回：无。"""
        from ecommerce.modules.account import service
        from ecommerce.shared.exceptions import NeedsAttention
        with patch.object(service, "check_login", return_value=False), patch.object(service, "save_session") as save:
            with self.assertRaises(NeedsAttention):
                service.authenticate(MagicMock(), MagicMock(), self.settings, False)
            save.assert_not_called()

    def test_click_failure_is_not_mistaken_for_same_tab(self):
        """点击失败不能被新窗口超时捕获并伪装成功；参数：无；返回：无。"""
        from ecommerce.infrastructure.browser.actions import click_and_get_next_page
        from playwright.sync_api import TimeoutError
        context, page, element = MagicMock(), MagicMock(), MagicMock()
        element.click.side_effect = TimeoutError("无法点击")
        with self.assertRaises(TimeoutError):
            click_and_get_next_page(context, page, element)
        element.click.assert_called_once()

    def test_browser_cleanup_on_failure(self):
        """业务失败时仍截图并关闭浏览器；参数：无；返回：无。"""
        from ecommerce.infrastructure.browser.session import browser_session
        context, page = MagicMock(), MagicMock()
        context.pages = [page]
        with patch("playwright.sync_api.sync_playwright") as factory:
            factory.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = context
            with self.assertRaisesRegex(RuntimeError, "业务失败"):
                with browser_session(self.settings):
                    raise RuntimeError("业务失败")
        context.close.assert_called_once()
        page.screenshot.assert_called_once()
