"""发布入口的新标签交接回归，所有请求由本地测试页面响应。"""
import os
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from ecommerce.infrastructure.browser import actions
from ecommerce.platforms.qianniu import product
from ecommerce.platforms.qianniu.verification import VerificationGuard


@unittest.skipUnless(os.environ.get("QIANNIU_BROWSER_TESTS") == "1", "显式启用本地浏览器测试")
class PublishNavigationBrowserTests(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<h1>测试发布页</h1>"))
        self.page = self.context.new_page()

    def test_slow_click_does_not_lose_opened_publish_tab(self):
        self.page.set_content('''<a id="publish" target="_blank" href="https://qianniu.test/publish"
            onclick="const end=performance.now()+250;while(performance.now()<end){}">发布商品</a>''')
        with VerificationGuard(self.context), patch.object(actions, "SHORT_TIMEOUT_MS", 80):
            result = actions.click_and_get_next_page(self.context, self.page, self.page.locator('#publish'))
        self.assertEqual(len(self.context.pages), 2)
        self.assertIsNot(result, self.page)
        self.assertEqual(result.url, "https://qianniu.test/publish")

    def test_same_tab_navigation_returns_original_page(self):
        self.page.set_content('<a id="publish" href="https://qianniu.test/same">发布商品</a>')
        with VerificationGuard(self.context), patch.object(actions, "SHORT_TIMEOUT_MS", 80):
            result = actions.click_and_get_next_page(self.context, self.page, self.page.locator('#publish'))
        self.assertIs(result, self.page)
        self.assertEqual(result.url, "https://qianniu.test/same")
        self.assertEqual(len(self.context.pages), 1)

    def test_missing_expected_popup_is_not_reported_as_same_tab(self):
        self.page.set_content('''<a id="publish" target="_blank" href="https://qianniu.test/publish"
            onclick="event.preventDefault();window.clickCount=(window.clickCount||0)+1">发布商品</a>''')
        with VerificationGuard(self.context), patch.object(actions, "SHORT_TIMEOUT_MS", 80):
            with self.assertRaisesRegex(PlaywrightTimeoutError, "要求打开新标签页"):
                actions.click_and_get_next_page(self.context, self.page, self.page.locator('#publish'))
        self.assertEqual(self.page.evaluate('clickCount'), 1)
        self.assertEqual(len(self.context.pages), 1)

    def test_blank_link_intercepted_as_same_tab_navigation(self):
        self.page.set_content('''<a id="publish" target="_blank" href="https://qianniu.test/intercepted"
            onclick="event.preventDefault();location.href=this.href">商品</a>''')
        with VerificationGuard(self.context), patch.object(actions, "SHORT_TIMEOUT_MS", 80):
            result = actions.click_and_get_next_page(self.context, self.page, self.page.locator('#publish'))
        self.assertIs(result, self.page)
        self.assertEqual(result.url, "https://qianniu.test/intercepted")
        self.assertEqual(len(self.context.pages), 1)

    def test_preexisting_tab_is_not_mistaken_for_new_publish_tab(self):
        existing = self.context.new_page()
        existing.goto('https://qianniu.test/old')
        self.page.set_content('<a id="publish" target="_blank" href="https://qianniu.test/new">发布商品</a>')
        with VerificationGuard(self.context):
            result = actions.click_and_get_next_page(self.context, self.page, self.page.locator('#publish'))
        self.assertIsNot(result, existing)
        self.assertIsNot(result, self.page)
        self.assertEqual(result.url, "https://qianniu.test/new")

    def test_zero_size_announcement_wrapper_does_not_block_publish(self):
        self.page.set_content('''<a data-index="2276" target="_blank"
            href="https://upload.taobao.com/auction/sell.jhtml" onclick="window.clickCount=(window.clickCount||0)+1">发布商品</a>
            <div class="next-overlay-wrapper opened" style="width:0;height:0">
              <div class="next-overlay-backdrop" style="position:fixed;inset:0;background:#8888;z-index:10"></div>
              <div role="dialog" style="position:fixed;left:60px;top:60px;width:300px;height:150px;z-index:11;background:white">
                <img alt="SKU管理页引导"><button aria-label="关闭"
                  onclick="window.closedNotice=true;this.closest('.next-overlay-wrapper').remove()">关闭</button>
              </div>
            </div>''')
        self.assertEqual(self.page.locator('.next-overlay-wrapper.opened:visible').count(), 0)
        with VerificationGuard(self.context), patch.object(product, "wait_for_page_ready"):
            result = product.click_publish_product(self.context, self.page)
        self.assertEqual(result.url, 'https://upload.taobao.com/auction/sell.jhtml')
        self.assertTrue(self.page.evaluate('closedNotice'))
        self.assertEqual(self.page.evaluate('clickCount'), 1)

    def test_unrecognized_dialog_is_never_closed_as_announcement(self):
        self.page.set_content('''<div role="dialog"><img alt="其他弹窗"><button aria-label="关闭"
            onclick="window.closedUnknown=true">关闭</button></div>''')
        with VerificationGuard(self.context):
            self.assertFalse(product._dismiss_sku_notice(self.page))
        self.assertTrue(self.page.get_by_role('dialog').is_visible())
        self.assertFalse(self.page.evaluate('!!window.closedUnknown'))

    def _brand_page(self):
        """构造两个独立输入框；仅浮层搜索完整名称才返回目标选项。"""
        self.page.goto("https://qianniu.test/category")
        self.page.set_content('''
            <input placeholder="可输入产品名称" value="粘毛器">
            <input id="brand" placeholder="请输入" value="无品牌" aria-expanded="false">
            <button id="unrelated" onclick="this.dataset.clicked='true'">无品牌/无注册商标</button>
            <div class="sell-o-select-popup-overlay" hidden>
              <div class="next-select-popup">
                <input id="brand-search" type="search"><div id="options">无品牌</div>
              </div>
            </div>
            <button id="next">确认，下一步</button>
            <script>
              const brand = document.querySelector('#brand');
              const popup = document.querySelector('.sell-o-select-popup-overlay');
              const search = document.querySelector('#brand-search');
              const options = document.querySelector('#options');
              const requested = '无品牌/无注册商标';
              brand.onclick = () => { popup.hidden = false; brand.setAttribute('aria-expanded', 'true'); };
              brand.oninput = () => { brand.dataset.edited = 'true'; };
              search.oninput = () => {
                options.textContent = '无品牌';
                if (search.value !== requested || window.noBrandMatch) return;
                setTimeout(() => {
                  const option = document.createElement('div');
                  option.className = 'options-item';
                  option.textContent = requested;
                  option.onclick = () => {
                    brand.value = window.wrongSelection ? '无品牌' : option.textContent;
                    popup.hidden = true;
                    brand.setAttribute('aria-expanded', 'false');
                  };
                  options.appendChild(option);
                }, 50);
              };
              document.querySelector('#next').onclick = () => {
                sessionStorage.setItem('brandResult', JSON.stringify({
                  selected: brand.value, query: search.value, directEdit: !!brand.dataset.edited,
                  unrelatedClick: !!document.querySelector('#unrelated').dataset.clicked,
                  productQuery: document.querySelector('input[placeholder="可输入产品名称"]').value
                }));
                location.href = '/editor';
              };
            </script>''')

    def test_brand_search_uses_popup_and_selects_full_match_before_navigation(self):
        self._brand_page()
        with VerificationGuard(self.context), patch.object(actions, "SHORT_TIMEOUT_MS", 80):
            result = product.select_brand_and_continue(self.page, "无品牌/无注册商标")
        self.assertEqual(result.url, "https://qianniu.test/editor")
        self.assertEqual(result.evaluate("JSON.parse(sessionStorage.getItem('brandResult'))"), {
            "selected": "无品牌/无注册商标", "query": "无品牌/无注册商标", "directEdit": False,
            "unrelatedClick": False, "productQuery": "粘毛器",
        })

    def test_missing_brand_does_not_click_outside_match_or_navigate(self):
        self._brand_page()
        self.page.evaluate("window.noBrandMatch = true")
        with VerificationGuard(self.context), patch.object(product, "BRAND_WAIT_MS", 500):
            with self.assertRaises(PlaywrightTimeoutError):
                product.select_brand_and_continue(self.page, "无品牌/无注册商标")
        self.assertEqual(self.page.url, "https://qianniu.test/category")
        self.assertIsNone(self.page.locator('#unrelated').get_attribute('data-clicked'))
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('brandResult')"))

    def test_brand_value_must_match_after_selection(self):
        self._brand_page()
        self.page.evaluate("window.wrongSelection = true")
        with VerificationGuard(self.context), patch.object(product, "BRAND_WAIT_MS", 600):
            with self.assertRaisesRegex(AssertionError, "核对已选品牌"):
                product.select_brand_and_continue(self.page, "无品牌/无注册商标")
        self.assertEqual(self.page.url, "https://qianniu.test/category")
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('brandResult')"))
