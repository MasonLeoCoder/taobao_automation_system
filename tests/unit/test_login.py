"""千牛新版工作台登录识别回归测试。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ecommerce.platforms.qianniu import login
from ecommerce.platforms.qianniu.identity import ShopIdentity
from ecommerce.modules.account import managed
from ecommerce.shared.exceptions import NeedsAttention


def page_with(url, visible=()):
    """创建模拟页面；参数：地址、可见选择器；返回：页面对象。"""
    page = MagicMock()
    page.url = url
    page.is_closed.return_value = False
    frame = MagicMock()
    frame.url = url

    def locator(selector):
        """模拟即时定位；参数：选择器；返回：定位器。"""
        result = MagicMock()
        result.count.return_value = int(selector in visible)
        result.nth.return_value.is_visible.return_value = selector in visible
        result.all.return_value = [result.nth.return_value] if selector in visible else []
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

    def test_only_confirmed_login_form_marks_session_expired(self):
        page = page_with(login.LOGIN_URL, ('#login',))
        self.assertIs(login.inspect_login(page), login.LoginStatus.REQUIRED)
        page.goto.return_value.status = 503
        self.assertIs(login.inspect_login(page), login.LoginStatus.UNCONFIRMED)

    def test_editor_login_iframe_requires_trusted_platform_origin(self):
        page = page_with('https://item.upload.taobao.com/sell/v2/publish.htm')
        page.frames += page_with('https://example.com/', ('#login',)).frames
        self.assertFalse(login.is_login_required(page))
        page.frames += page_with(login.LOGIN_URL, ('#login',)).frames
        self.assertTrue(login.is_login_required(page))

    def test_new_tab_is_returned(self):
        """扫码后新标签页的工作台能被识别；参数：无；返回：无。"""
        original = page_with(login.LOGIN_URL)
        home = page_with(login.HOME_URL, ('text="首页"', 'text="交易"'))
        original.context.pages = [original, home]
        self.assertIs(login._wait_for_login_success(original), home)

    def test_timeout_does_not_restart_qrcode(self):
        """超时不会自动跳回登录页；参数：无；返回：无。"""
        page = page_with(login.LOGIN_URL)
        with patch.object(login, "_wait_for_scan_entry", return_value=(None, MagicMock())), patch.object(login, "_wait_for_login_success", side_effect=login.PlaywrightTimeoutError("等待超时")):
            with self.assertRaises(login.PlaywrightTimeoutError):
                login.login_with_qrcode(page)
        page.goto.assert_called_once()
        page.reload.assert_not_called()

    def test_scan_waits_for_feedback_and_reprompts_if_login_not_confirmed(self):
        """扫码后先等待人工反馈；早反馈后不跳转或重新点击二维码。"""
        page = page_with(login.LOGIN_URL)
        home = page_with(login.HOME_URL, ('text="首页"', 'text="交易"'))
        opened = []
        button = MagicMock()
        button.click.side_effect = lambda: opened.append('qr')
        def poll(current, *, wait_ms):
            self.assertIs(current, page)
            if opened == ['qr', False]:
                raise login.PlaywrightTimeoutError('尚未登录')
            self.assertEqual(opened, ['qr', False, True])
            return home
        with patch.object(login, '_wait_for_scan_entry', return_value=(None, button)), patch.object(login, '_wait_for_login_success', side_effect=poll):
            self.assertIs(login.login_with_qrcode(page, on_wait=lambda current, retry: opened.append(retry)), home)
        button.click.assert_called_once()
        page.goto.assert_called_once()

    def test_workbench_navigation_while_waiting_for_qrcode(self):
        """等待扫码按钮时跳转到工作台，应直接返回并交给调用者核验账号。"""
        page = page_with(login.HOME_URL, ('text="首页"', 'text="交易"'))
        page.url = login.LOGIN_URL
        page.wait_for_timeout.side_effect = lambda _ms: setattr(page, 'url', login.HOME_URL)
        on_wait = MagicMock()
        self.assertIs(login.login_with_qrcode(page, on_wait=on_wait), page)
        page.wait_for_timeout.assert_called_once()
        on_wait.assert_not_called()

    def test_workbench_opens_in_new_tab_during_qrcode_wait(self):
        page = page_with(login.LOGIN_URL)
        home = page_with(login.HOME_URL, ('text="首页"', 'text="交易"'))
        page.wait_for_timeout.side_effect = lambda _ms: page.context.pages.append(home)
        self.assertIs(login.login_with_qrcode(page), home)
        page.wait_for_timeout.assert_called_once()

    def test_transition_can_complete_during_qrcode_click(self):
        page = page_with(login.LOGIN_URL, (login.QRCODE_SELECTOR,))
        button = page.frames[0].locator(login.QRCODE_SELECTOR).nth.return_value
        home = page_with(login.HOME_URL, ('text="首页"', 'text="交易"'))

        def interrupted_click():
            page.context.pages.append(home)
            raise login.PlaywrightError('扫码入口因页面跳转而消失')

        with patch.object(login, '_wait_for_scan_entry', return_value=(None, button)):
            button.click.side_effect = interrupted_click
            self.assertIs(login.login_with_qrcode(page), home)
        button.click.assert_called_once()

    def test_managed_verification_reads_only_the_selected_identity(self):
        page, on_expiry = MagicMock(), MagicMock()
        identity = ShopIdentity('123456789', '示例店铺', '示例-运营', 'tb1234567890:示例')
        target = {'platform_shop_id': identity.shop_id, 'login_name': identity.login_name}
        with (patch.object(managed, 'inspect_login', return_value=login.LoginStatus.AUTHENTICATED),
              patch.object(managed, 'read_shop_identity', return_value=identity) as read):
            self.assertEqual(managed.verify_session(page, target, on_expiry), identity)
            with self.assertRaisesRegex(NeedsAttention, '店铺不符'):
                managed.verify_session(page, {**target, 'platform_shop_id': '987654321'}, on_expiry)
        read.assert_called_with(page)
        on_expiry.assert_not_called()

    def test_managed_verification_only_marks_explicit_login_requirement(self):
        for status in (login.LoginStatus.UNCONFIRMED, login.LoginStatus.REQUIRED):
            with self.subTest(status=status), patch.object(managed, 'inspect_login', return_value=status):
                on_expiry = MagicMock(side_effect=NeedsAttention('明确需要重新登录'))
                with self.assertRaises(NeedsAttention):
                    managed.verify_session(MagicMock(), {}, on_expiry)
                self.assertEqual(on_expiry.called, status is login.LoginStatus.REQUIRED)

    def test_managed_verification_requires_complete_login_name(self):
        with (patch.object(managed, 'inspect_login', return_value=login.LoginStatus.AUTHENTICATED),
              patch.object(managed, 'read_shop_identity', return_value=ShopIdentity('123456789', '示例店铺', '示例-运营'))):
            with self.assertRaisesRegex(NeedsAttention, '无法核实'):
                managed.verify_session(MagicMock(), {}, MagicMock())
