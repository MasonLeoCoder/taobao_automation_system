"""离线模拟淘宝首页账号菜单，核对完整昵称的读取。"""
import os
from unittest import TestCase, skipUnless

from playwright.sync_api import sync_playwright

from ecommerce.platforms.taobao.identity import read_buyer_nickname
from ecommerce.platforms.taobao.login import HOME_URL


@skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS') == '1', '显式启用本地浏览器测试')
class BuyerIdentityBrowserTests(TestCase):
    def setUp(self):
        runtime = sync_playwright().start()
        self.addCleanup(runtime.stop)
        browser = runtime.chromium.launch(headless=True)
        self.addCleanup(browser.close)
        self.page = browser.new_page()

    def serve_menu(self, title, card):
        html = f'''<meta charset="utf-8"><style>#card {{display:none}} #account:hover #card {{display:block}}</style>
            <header id="account"><a class="site-nav-login-info-nick">{title}</a>
              <div id="card"><img alt="头像"><strong>{card}</strong><div><a>账号管理</a><a>退出</a></div></div>
            </header><main>网页正文</main>'''
        self.page.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=html))
        self.page.goto(HOME_URL)

    def test_abbreviated_navigation_uses_only_full_menu_name(self):
        self.serve_menu('示例昵称…', '示例昵称甲乙丙丁')
        self.assertEqual(read_buyer_nickname(self.page), '示例昵称甲乙丙丁')

    def test_conflicting_menu_is_not_accepted(self):
        self.serve_menu('示例昵称甲', '示例昵称乙')
        self.assertIsNone(read_buyer_nickname(self.page))
