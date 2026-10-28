"""全站公共控件验证，所有请求仅进入临时Flask和隔离数据。"""
import os
from unittest.mock import patch
import unittest

from playwright.sync_api import expect
from tests.integration import test_web_collection_browser as fixtures


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS') == '1', '显式启用全站公共控件验证')
class FormControlsBrowserTests(unittest.TestCase):
    def setUp(self):
        """复用隔离网页与任务替身；参数：无；返回：无。"""
        self.host = fixtures.WebCollectionBrowserTests()
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)
        self.fixture, self.page = self.host.fixture, self.host.page
        mocked = patch('ecommerce.infrastructure.storage.search_analysis.collection_store', return_value=self.fixture.store)
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_all_pages_use_one_control_palette_and_height(self):
        """各页的可见筛选、日期、下拉、范围和分页接入相同公共控件。"""
        for path in ('/collections/searches', '/collections/details', '/logs', '/modules/collection',
                     '/analysis/searches', '/collections/searches/products?category=%E7%B2%98%E6%AF%9B%E5%99%A8',
                     '/collections/searches/' + self.fixture.task, '/collections/products/123?module=parameters', '/tasks'):
            with self.subTest(path=path):
                response = self.page.goto('http://127.0.0.1' + path)
                self.assertEqual(response.status, 200)
                nodes = self.page.locator('main input:not([type="hidden"]),main select')
                missing = nodes.evaluate_all('(nodes)=>nodes.filter(n=>!n.matches(".ui-control,.ui-choice")).map(n=>n.outerHTML)')
                self.assertEqual(missing, [])
                self.assertEqual(self.page.locator('link[href$="form_controls.css"]').count(), 1)
                heights = nodes.evaluate_all('(nodes)=>nodes.filter(n=>n.getClientRects().length&&!n.matches(".ui-choice")&&!n.closest(".ui-range")).map(n=>({height:n.getBoundingClientRect().height,id:n.id,name:n.name}))')
                for row in heights:
                    self.assertAlmostEqual(row['height'], 40, delta=1, msg=str(row))
        self.assertEqual(self.host.errors, [])

    def test_changing_public_tokens_updates_all_controls(self):
        """修改公共变量即可同步改变同页全部控件及搜索控件，页面没有旧尺寸覆盖。"""
        for path in ('/logs', '/modules/collection', '/analysis/searches'):
            self.page.goto('http://127.0.0.1' + path)
            self.page.evaluate("document.documentElement.style.setProperty('--control-height','48px');document.documentElement.style.setProperty('--control-radius','14px')")
            values = self.page.locator('main .ui-control').evaluate_all('(nodes)=>nodes.filter(n=>n.getClientRects().length).map(n=>({height:n.getBoundingClientRect().height,radius:getComputedStyle(n).borderRadius}))')
            self.assertTrue(values)
            for row in values:
                self.assertAlmostEqual(row['height'], 48, delta=1)
                self.assertEqual(row['radius'], '14px')

    def test_native_filter_and_disabled_collection_rules_are_unchanged(self):
        """原生下拉选择、复选框禁用联动及表单条件继续使用现有行为。"""
        self.page.goto('http://127.0.0.1/logs')
        level = self.page.get_by_label('日志级别', exact=True)
        level.select_option('WARNING')
        self.page.get_by_role('button', name='查询日志', exact=True).click()
        self.assertIn('level=WARNING', self.page.url)
        self.page.goto('http://127.0.0.1/modules/collection')
        limit = self.page.get_by_label('每页最多商品数', exact=True)
        expect(limit).to_be_disabled()
        self.page.get_by_label('不限数量', exact=True).uncheck()
        expect(limit).to_be_enabled()
        self.page.get_by_role('tab', name='商品详情采集', exact=True).click()
        self.page.get_by_role('button', name='取消全选', exact=True).click()
        review = self.page.get_by_label('评价数量范围', exact=True)
        expect(review).to_be_disabled()
        self.page.get_by_label('评价明细', exact=True).check()
        expect(review).to_be_enabled()
        review.select_option('all')
        self.assertEqual(review.input_value(), 'all')
        self.assertEqual(self.host.errors, [])

    def test_mobile_layout_and_native_picker_style(self):
        """窄屏各页不产生整页横向溢出，普通下拉使用相同选中主题。"""
        for width in (390, 768, 1480):
            self.page.set_viewport_size({'width':width,'height':1000})
            for path in ('/collections/details', '/logs', '/modules/collection', '/analysis/searches'):
                self.page.goto('http://127.0.0.1' + path)
                size = self.page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})')
                self.assertLessEqual(size['scroll'], size['width'] + 1, path + ': ' + str(size))
                if path == '/analysis/searches':
                    self.page.locator('#sa-snapshot').evaluate("n=>n.selectedOptions[0].textContent='日期范围汇总 · 每件商品取最新 · 较长范围名称'")
                    overlaps = self.page.locator('.sa-filter-top').evaluate('''n=>{
                        const rects=[...n.children].map(n=>n.getBoundingClientRect());
                        return rects.flatMap((a,i)=>rects.slice(i+1).filter(b=>Math.min(a.right,b.right)-Math.max(a.left,b.left)>1 && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1));
                    }''')
                    self.assertEqual(overlaps, [], f'分析筛选字段互相重叠：{width}px')
        self.page.goto('http://127.0.0.1/logs')
        level = self.page.get_by_label('日志级别', exact=True)
        level.click()
        level.get_by_role('option', name='警告', exact=True).click()
        self.assertEqual(level.input_value(), 'WARNING')
        self.assertEqual(self.host.errors, [])
