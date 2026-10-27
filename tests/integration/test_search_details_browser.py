"""本地浏览器验证搜索页保留、多商品九模块调度、跨页复用及中断；不访问真实淘宝。"""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection import service, detail_service
from ecommerce.platforms.taobao import session
from ecommerce.shared.config import load_settings, DEFAULT_TAOBAO_TIMING
from ecommerce.shared.exceptions import TaskStopped
from tests.integration.test_taobao_collection_browser import HOME
from tests.integration.test_taobao_pagination_browser import paginated_html


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用本地页面验证')
class SearchDetailsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """建立本地页面浏览器；参数：类；返回：无。"""
        cls.runtime=sync_playwright().start()
        cls.browser=cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        """结束隔离浏览器；参数：类；返回：无。"""
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        """拦截全部请求并使用隔离文件；参数：无；返回：无。"""
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings=replace(load_settings(root=Path(temporary.name)).for_platform('taobao'),headless=True,
            timeout_ms=1500,taobao_timing={key:(0,0) for key in DEFAULT_TAOBAO_TIMING})
        self.context=self.browser.new_context()
        self.addCleanup(self.context.close)
        self.requests=[]
        def route(request):
            """只返回本地夹具；参数：请求；返回：本地响应。"""
            self.requests.append(request.request.url)
            url=urlsplit(request.request.url)
            if url.hostname=='s.taobao.com':
                body=paginated_html(total=2)+'<script>document.querySelector("input[name=q]").value='+json.dumps(parse_qs(url.query)['q'][0])+'</script>'
            elif url.hostname in {'item.taobao.com','detail.tmall.com'}:
                body='<title>本地商品详情</title><main>本地详情 '+parse_qs(url.query)['id'][0]+'</main>'
            else:
                body=HOME
            request.fulfill(status=200,content_type='text/html; charset=utf-8',body=body)
        self.context.route('**/*',route)
        self.home=self.context.new_page()
        self.home.goto('https://www.taobao.com/')
        self.calls=[]
        self.fail=False
        self.change_list=False
        def collect(name):
            """模块替身只隔离采集内容，真实执行页面导航、入口落盘和九步汇总。"""
            def execute(settings,result,**kwargs):
                self.calls.append((result.target.item_id,name,kwargs))
                if self.fail:
                    raise TaskStopped('本地模拟人工停止')
                if self.change_list and name=='questions':
                    page=next(p for p in self.context.pages if urlsplit(p.url).hostname=='s.taobao.com')
                    page.locator('input[name=q]').fill('已改变')
                return {'status':'complete','video_status':'no_video','message':'本地模块替身完成',
                        'record_path':str(settings.workspace/'fixture.json')}
            return execute
        self.module=SimpleNamespace(sku_continuation_allowed=lambda value:True,
            **{function:collect(name) for name,function in [('basic_info','collect_basic_info'),('video','collect_main_video'),('main_images','collect_main_images'),
                ('skus','collect_skus'),('parameters','collect_parameters'),('detail_images','collect_detail_images'),
                ('review_summary','collect_review_summary'),('reviews','collect_reviews'),('questions','collect_questions')]})

    def run_collection(self,attention=None):
        """运行实际搜索与详情协作，仅替换九模块内容；参数：反馈；返回：业务说明。"""
        @contextmanager
        def browser(settings,**kwargs):
            yield self.context,self.home
        with patch.object(session,'base_browser_session',browser),patch.object(detail_service,'import_module',return_value=self.module):
            return service.collect_keyword(self.settings,'粘毛器',pages=2,limit=2,with_details=True,wait=False,on_attention=attention)

    def summary(self):
        """读取本轮唯一汇总；参数：无；返回：JSON。"""
        return json.loads(next((self.settings.workspace/'output/reports/taobao_list').glob('*.json')).read_text(encoding='utf-8'))

    def test_three_distinct_products_from_two_pages_dispatch_all_modules_and_keep_search(self):
        """两页四处展示产生三个独立入口与运行，九模块各执行三次，跨页重复只复用。"""
        self.run_collection()
        summary=self.summary()
        self.assertEqual((summary['schema_version'],summary['completed_page_count'],summary['detail_success_count'],summary['detail_reused_count']),(2,2,3,1))
        self.assertEqual(len(self.calls),27)
        self.assertEqual([name for identifier,name,kwargs in self.calls[:9]],
                         ['basic_info','video','main_images','skus','parameters','detail_images','review_summary','reviews','questions'])
        self.assertTrue(all(not kwargs.get('all_reviews',False) and not kwargs.get('all_questions',False) for _,_,kwargs in self.calls))
        detail_requests=[url for url in self.requests if urlsplit(url).hostname in {'item.taobao.com','detail.tmall.com'}]
        self.assertEqual(len(detail_requests),3)
        self.assertEqual(len([url for url in self.requests if urlsplit(url).hostname=='s.taobao.com']),1)
        page=next(p for p in self.context.pages if urlsplit(p.url).hostname=='s.taobao.com')
        self.assertEqual(page.evaluate('nextClicks'),1)
        self.assertEqual(len(self.context.pages),2)
        entries=[json.loads(p.read_text(encoding='utf-8')) for p in (self.settings.workspace/'output/raw/taobao_detail_open').glob('*.json')]
        reports=[json.loads(p.read_text(encoding='utf-8')) for p in (self.settings.workspace/'output/raw/taobao_details').glob('*/history/*/collection_summary.json')]
        self.assertEqual((len(entries),len(reports)),(3,3))
        self.assertEqual({e['entry_key'] for e in entries},{r['entry_key'] for r in reports})
        self.assertTrue(all(e['input_type']=='url' and e['list_source'] for e in entries))

    def test_stopping_first_module_keeps_list_and_interrupts_without_next_page(self):
        """第一商品中断，第一页列表仍保存，后续模块、商品与翻页均不执行。"""
        self.fail=True
        with self.assertRaises(TaskStopped):
            self.run_collection()
        summary=self.summary()
        self.assertEqual((summary['status'],summary['completed_page_count'],summary['detail_success_count']),('interrupted',1,0))
        self.assertEqual(len(self.calls),1)
        page=next(p for p in self.context.pages if urlsplit(p.url).hostname=='s.taobao.com')
        self.assertEqual(page.evaluate('nextClicks'),0)
        self.assertEqual(summary['detail_attempts'][0]['status'],'interrupted')

    def test_failed_product_entry_does_not_block_later_products_or_next_page(self):
        """单商品入口业务超时标红跳过，后续商品和翻页继续；关闭浏览器仍由停止用例覆盖。"""
        from ecommerce.modules.collection import search_details
        from ecommerce.shared.exceptions import ModuleBlocked
        original=search_details.open_list_detail
        attempts=[]
        def open_entry(*args,**kwargs):
            attempts.append(args[2])
            if len(attempts)==1:raise ModuleBlocked('合成详情入口无进展')
            return original(*args,**kwargs)
        with patch.object(search_details,'open_list_detail',side_effect=open_entry):
            self.run_collection()
        summary=self.summary()
        self.assertEqual(summary['status'],'complete_with_errors')
        self.assertEqual(summary['completed_page_count'],2)
        self.assertEqual(summary['detail_success_count'],2)
        self.assertEqual(summary['detail_attempts'][0]['status'],'failed_skipped')
        self.assertEqual(len(self.calls),18)

    def test_changed_search_conditions_preserve_saved_detail_success_but_stop_workflow(self):
        """返回时搜索词变化导致暂停，已成功详情仍计入保存数量，流程不继续翻页。"""
        self.change_list=True
        def attention(*args):
            raise TaskStopped('本地停止条件已改变的现场')
        with self.assertRaises(TaskStopped):
            self.run_collection(attention)
        summary=self.summary()
        self.assertEqual((summary['status'],summary['detail_success_count']),('interrupted',1))
        self.assertEqual(summary['detail_attempts'][0]['detail_status'],'complete')
        self.assertEqual(len(self.calls),9)
