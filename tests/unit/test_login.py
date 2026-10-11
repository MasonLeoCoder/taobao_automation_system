"""千牛新版工作台登录识别回归测试。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ecommerce.platforms.qianniu import login
from ecommerce.modules.account import service
from ecommerce.shared.exceptions import NeedsAttention


def page_with(url, visible=()):
    """创建模拟页面；参数：地址、可见选择器；返回：页面对象。"""
    page = MagicMock()
    page.url = url
    page.is_closed.return_value = False
    frame = MagicMock()

    def locator(selector):
        """模拟即时定位；参数：选择器；返回：定位器。"""
        result = MagicMock()
        result.count.return_value = int(selector in visible)
        result.nth.return_value.is_visible.return_value = selector in visible
        return result

    frame.locator.side_effect = locator
    page.frames = [frame]
    page.context.pages = [page]
    page.goto.return_value.status = 200
    return page


class LoginTests(unittest.TestCase):
    """覆盖新版导航、错误页面、新标签页和超时行为。"""

    def test_new_home_and_trade_navigation(self):
        """没有旧选择器时识别用户提供的工作台；参数：无；返回：无。"""
        page = page_with(login.HOME_URL + "/", ('text="首页"', 'text="交易"'))
        self.assertTrue(login._is_authenticated(page))
        self.assertTrue(login.check_login(page))

    def test_url_alone_and_other_hosts_not_authenticated(self):
        """空白工作台或其他域名不判定成功；参数：无；返回：无。"""
        self.assertFalse(login._is_authenticated(page_with(login.HOME_URL)))
        self.assertFalse(login._is_authenticated(page_with("https://example.com/home.htm/", ('text="首页"', 'text="交易"'))))

    def test_login_form_overrides_stale_navigation(self):
        """登录表单优先于残留导航；参数：无；返回：无。"""
        page = page_with(login.HOME_URL, ('text="首页"', 'text="交易"', '#login'))
        self.assertFalse(login._is_authenticated(page))
        self.assertFalse(login.check_login(page))

    def test_new_tab_is_returned(self):
        """扫码后新标签页的工作台能被识别；参数：无；返回：无。"""
        original = page_with(login.LOGIN_URL)
        home = page_with(login.HOME_URL, ('text="首页"', 'text="交易"'))
        original.context.pages = [original, home]
        self.assertIs(login._wait_for_login_success(original), home)

    def test_timeout_does_not_restart_qrcode(self):
        """超时不会自动跳回登录页；参数：无；返回：无。"""
        page = page_with(login.LOGIN_URL)
        with patch.object(login, "wait_for_selector_any_frame"), patch.object(login, "_wait_for_login_success", side_effect=login.PlaywrightTimeoutError("等待超时")):
            with self.assertRaises(login.PlaywrightTimeoutError):
                login.login_with_qrcode(page)
        page.goto.assert_called_once()
        page.reload.assert_not_called()

    def test_authenticate_verifies_new_tab_and_saves(self):
        """业务在扫码成功页复核并保存会话；参数：无；返回：无。"""
        original, home, context, settings = MagicMock(), MagicMock(), MagicMock(), MagicMock(headless=False)
        with patch.object(service, "check_login", side_effect=[False, True]) as check, patch.object(service, "login_with_qrcode", return_value=home), patch.object(service, "save_session") as save:
            self.assertIs(service.authenticate(context, original, settings), home)
            self.assertIs(check.call_args.args[0], home)
            save.assert_called_once_with(context, settings)

    def test_scan_timeout_needs_attention_without_save(self):
        """扫码未确认时不保存错误状态；参数：无；返回：无。"""
        with patch.object(service, "check_login", return_value=False), patch.object(service, "login_with_qrcode", side_effect=login.PlaywrightTimeoutError("扫码未确认")), patch.object(service, "save_session") as save:
            with self.assertRaises(NeedsAttention):
                service.authenticate(MagicMock(), MagicMock(), MagicMock(headless=False))
            save.assert_not_called()
