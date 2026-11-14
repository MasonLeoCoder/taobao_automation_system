"""在本地模拟千牛店铺信息页，验证店铺和登录者的读取。"""

import os
import unittest

from playwright.sync_api import sync_playwright

from ecommerce.platforms.qianniu import login
from ecommerce.platforms.qianniu.identity import SHOP_INFORMATION_URL, ShopIdentity, read_shop_identity
from ecommerce.platforms.qianniu.verification import VerificationGuard


@unittest.skipUnless(os.environ.get('QIANNIU_BROWSER_TESTS') == '1', '显式启用本地浏览器测试')
class QianniuIdentityBrowserTests(unittest.TestCase):
    def setUp(self):
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(headless=True)
        self.addCleanup(browser.close)
        self.page = browser.new_page()

    def serve_shop(self, account, shop_rows=None, owner='tb1234567890'):
        if shop_rows is None:
            shop_rows = '<div><div class="label--sample">店铺ID</div><div>123456789<button aria-label="复制"></button></div></div>'
        html = f'''<meta charset="utf-8">
            <header><div class="accountWrap--sample">
                <div class="shopName--sample">示例店铺 <div hidden>退出</div></div>
                <div class="nameWrap--sample">{account}</div>
            </div></header>
            <main>
                <div><div class="label--sample">淘宝账号名</div><div>{owner}</div></div>
                {shop_rows}
            </main>'''
        self.page.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=html))

    def test_main_account(self):
        self.serve_shop('tb1234567890')
        self.assertEqual(read_shop_identity(self.page),
                         ShopIdentity('123456789', '示例店铺', 'tb1234567890', 'tb1234567890'))

    def test_subaccount_is_read_from_header(self):
        self.serve_shop('<div>示例</div><div>运营</div>')
        self.assertEqual(read_shop_identity(self.page),
                         ShopIdentity('123456789', '示例店铺', '示例-运营', 'tb1234567890:示例'))

    def test_subaccount_role_does_not_change_login_name(self):
        self.serve_shop('<div>示例</div><div>客服</div>')
        self.assertEqual(read_shop_identity(self.page),
                         ShopIdentity('123456789', '示例店铺', '示例-客服', 'tb1234567890:示例'))

    def test_missing_shop_login_name_does_not_guess_subaccount(self):
        self.serve_shop('<div>示例</div><div>运营</div>', owner='')
        self.assertEqual(read_shop_identity(self.page), ShopIdentity('123456789', '示例店铺', '示例-运营'))

    def test_ambiguous_shop_id_is_not_accepted(self):
        self.serve_shop('tb1234567890', '''
            <div><div class="label--sample">店铺ID</div><div>123456789</div></div>
            <div><div class="label--sample">店铺ID</div><div>987654321</div></div>''')
        self.assertIsNone(read_shop_identity(self.page, timeout_ms=700))

    def test_redirect_to_login_has_no_identity(self):
        self.page.route('**/*', lambda route: route.fulfill(status=200, body='login'))
        self.page.route(SHOP_INFORMATION_URL, lambda route: route.fulfill(
            status=302, headers={'location': 'https://loginmyseller.taobao.com/'}))
        self.assertIsNone(read_shop_identity(self.page, timeout_ms=2_000))

    def test_late_redirect_to_workbench_does_not_wait_for_qrcode(self):
        self.page.route(login.HOME_URL, lambda route: route.fulfill(
            content_type='text/html; charset=utf-8', body='<nav>首页</nav><nav>交易</nav>'))
        self.page.route(login.LOGIN_URL, lambda route: route.fulfill(
            content_type='text/html; charset=utf-8',
            body=f'<script>setTimeout(() => location.href = "{login.HOME_URL}", 500)</script>'))

        def unexpected_attention(message):
            self.fail(message)

        with VerificationGuard(self.page.context, unexpected_attention):
            self.assertIs(login.login_with_qrcode(self.page), self.page)
