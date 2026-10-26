"""真实隔离DOM验证基本信息读取；不访问淘宝，不存在SKU区域也能采集。"""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from playwright.sync_api import sync_playwright
from ecommerce.modules.collection.detail_basic_info import collect_basic_info
from ecommerce.platforms.taobao.detail import DetailPage,resolve_detail_input
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.config import load_settings,DEFAULT_TAOBAO_TIMING


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用隔离基本信息浏览器测试')
class BasicInfoBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """启动隔离浏览器；参数：无；返回：无。"""
        cls.runtime=sync_playwright().start();cls.browser=cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        """释放浏览器；参数：无；返回：无。"""
        cls.browser.close();cls.runtime.stop()

    def setUp(self):
        """建立纯本地详情样例；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=replace(load_settings(root=Path(temp.name)),headless=True,timeout_ms=1200,
                              taobao_timing={key:(0,0) for key in DEFAULT_TAOBAO_TIMING})
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.html='''<html><body><div class="ItemTitle--abc"><h1 class="mainTitle--abc">独立基本信息商品</h1><p>已售 2000+</p></div>
            <div class="shopName--abc">样例店铺</div><div class="highlightPrice--abc">券后 ¥3.9 起</div>
            <div style="display:none" class="ItemTitle--old"><h1 class="mainTitle--old">隐藏旧标题</h1></div>
            <script>window.clicks=0;document.addEventListener('click',()=>window.clicks++)</script></body></html>'''
        self.requests=[]
        def route(route):
            self.requests.append(route.request.url)
            route.fulfill(content_type='text/html; charset=utf-8',body=self.html)
        self.context.route('**/*',route)
        self.page=self.context.new_page();self.page.goto('https://item.taobao.com/item.htm?id=123')
        self.result=DetailPage(self.page,resolve_detail_input('123'),'本地详情样例')

    def collect(self):
        """在真实守卫下采集；参数：无；返回：本地资料。"""
        with TaobaoGuard(self.context,self.settings.log_dir,timing=self.settings.taobao_timing):
            return collect_basic_info(self.settings,self.result)

    def test_no_sku_dom_no_click_and_no_extra_network(self):
        result=self.collect()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['product_info']['title'],'独立基本信息商品')
        self.assertEqual(result['product_info']['sales_text'],'已售 2000+')
        self.assertEqual(result['product_info']['shop_name'],'样例店铺')
        self.assertEqual(result['product_info']['price_text'],'券后 ¥3.9 起')
        self.assertEqual(self.page.evaluate('window.clicks'),0)
        self.assertEqual(len(self.requests),1)

    def test_ambiguous_title_is_partial_and_does_not_clear_effective_basic(self):
        first=self.collect();before=json.loads(Path(first['record_path']).read_text(encoding='utf-8'))
        self.page.locator('body').evaluate("e=>e.insertAdjacentHTML('beforeend','<div class=ItemTitle--other><h1 class=mainTitle--other>不同标题</h1></div>')")
        second=self.collect();after=json.loads(Path(second['record_path']).read_text(encoding='utf-8'))
        self.assertEqual(second['status'],'partial')
        self.assertEqual(second['product_info']['field_states']['title'],'ambiguous')
        self.assertEqual(before['product_info'],after['product_info'])

    def test_missing_optional_values_remain_null_with_states(self):
        self.page.locator('.shopName--abc,.highlightPrice--abc').evaluate_all('nodes=>nodes.forEach(node=>node.remove())')
        result=self.collect()
        self.assertEqual(result['status'],'complete')
        self.assertIsNone(result['product_info']['shop_name'])
        self.assertEqual(result['product_info']['field_states']['price_text'],'not_displayed')


if __name__=='__main__':unittest.main()
