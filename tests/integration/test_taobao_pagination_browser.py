"""本地页面验证逐页保存、一次翻页、末页和中断；不访问真实淘宝。"""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection.service import collect_keyword
from ecommerce.platforms.taobao import session
from ecommerce.shared.config import load_settings,DEFAULT_TAOBAO_TIMING
from ecommerce.shared.exceptions import NeedsAttention,TaskStopped
from tests.integration.test_taobao_collection_browser import HOME,card_html,results_html


def paginated_html(total=3,*,noop=False,reset_sort=False,dialog=False):
    """构造已适配分页；参数：总页数及异常开关；返回：HTML。"""
    rows={number:''.join(card_html('123456' if number<3 and index==0 else str(234560+number*10+index),title=f'第{number}页商品{index+1}') for index in range(4)) for number in range(1,total+1)}
    body=results_html(rows[1]).replace('<div class="next-pagination-display"><em>1</em>/100</div>','')
    body+=f'<div class="next-pagination next-medium next-normal"><span class="next-pagination-display"><em>1</em>/{total}</span><button class="next-next">下一页</button></div>'
    body+='''<script>
      window.nextClicks=0;window.pageIndex=1;
      const pages=__PAGES__,total=__TOTAL__;
      document.querySelector('.next-next').disabled=total===1;
      document.querySelector('.next-next').onclick=()=>{
        window.nextClicks++;
        if(__NOOP__)return;
        document.querySelector('#content_items_wrapper').setAttribute('aria-busy','true');
        setTimeout(()=>{
          window.pageIndex++;
          history.replaceState({},'',location.pathname+'?q='+encodeURIComponent('粘毛器')+'&page='+window.pageIndex);
          document.querySelector('.next-pagination-display em').textContent=window.pageIndex;
          document.querySelector('#content_items_wrapper').innerHTML=pages[window.pageIndex];
          document.querySelector('#content_items_wrapper').removeAttribute('aria-busy');
          document.querySelector('.next-next').disabled=window.pageIndex>=total;
          if(__RESET__){document.querySelectorAll('.next-tabs-tab').forEach(e=>e.classList.remove('active'));document.querySelector('.next-tabs-tab').classList.add('active');}
          if(__DIALOG__)document.body.insertAdjacentHTML('beforeend','<div role="dialog" style="position:fixed;inset:0;background:white">待人工核对的本地窗口</div>');
        },40);
      };
    </script>'''.replace('__PAGES__',json.dumps(rows,ensure_ascii=False)).replace('__TOTAL__',str(total)).replace('__NOOP__',str(noop).lower()).replace('__RESET__',str(reset_sort).lower()).replace('__DIALOG__',str(dialog).lower())
    return body


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用本地浏览器测试')
class PaginationBrowserTests(unittest.TestCase):
    """隔离临时浏览器与输出，每项按真实DOM行为核对。"""
    @classmethod
    def setUpClass(cls):
        """启动仅访问本地响应的浏览器；参数：类；返回：无。"""
        cls.runtime=sync_playwright().start()
        cls.browser=cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        """结束本地浏览器；参数：类；返回：无。"""
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        """为当前用例建立隔离页面和文件；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings=replace(load_settings(root=Path(temp.name)).for_platform('taobao'),headless=True,timeout_ms=1500,taobao_timing={key:(0,0) for key in DEFAULT_TAOBAO_TIMING})
        self.context=self.browser.new_context()
        self.addCleanup(self.context.close)
        self.body=paginated_html()
        def route(request):
            url=urlsplit(request.request.url)
            content=self.body if url.hostname=='s.taobao.com' else HOME
            if url.hostname=='s.taobao.com':
                content+='<script>document.querySelector("input[name=q]").value='+json.dumps(parse_qs(url.query)['q'][0])+'</script>'
            request.fulfill(status=200,content_type='text/html; charset=utf-8',body=content)
        self.context.route('**/*',route)
        self.home=self.context.new_page()
        self.home.goto('https://www.taobao.com/')

    def run_collection(self,*,pages=3,on_attention=None):
        """走正式采集流程，只替换浏览器工厂；参数：页数、人工处理；返回：消息。"""
        @contextmanager
        def browser(settings):
            yield self.context,self.home
        with patch.object(session,'base_browser_session',browser):
            return collect_keyword(self.settings,'粘毛器',limit=3,pages=pages,wait=False,on_attention=on_attention)

    def search_page(self):
        """取得本地搜索页；参数：无；返回：Page。"""
        return next(p for p in self.context.pages if urlsplit(p.url).hostname=='s.taobao.com')

    def payloads(self):
        """读取每页原件；参数：无；返回：按页码排序的批次。"""
        return sorted([json.loads(p.read_text(encoding='utf-8')) for p in (self.settings.workspace/'output/raw/taobao_products').glob('*.json')],key=lambda v:v['page_number'])

    def summary(self):
        """读取独立汇总；参数：无；返回：汇总对象。"""
        paths=list((self.settings.workspace/'output/reports/taobao_list').glob('*.json'))
        self.assertEqual(len(paths),1)
        return json.loads(paths[0].read_text(encoding='utf-8'))

    def test_three_pages_each_three_keeps_duplicate_products_and_sorts_once(self):
        """一任务三批九条，跨页重复保留，排序及翻页不重放。"""
        self.run_collection()
        data=self.payloads()
        self.assertEqual([v['page_number'] for v in data],[1,2,3])
        self.assertEqual([v['collected_count'] for v in data],[3,3,3])
        self.assertEqual(len({v['task_id'] for v in data}),1)
        self.assertEqual(len({v['batch_id'] for v in data}),3)
        self.assertEqual(data[0]['items'][0]['product_id'],data[1]['items'][0]['product_id'])
        self.assertEqual([v['pagination_performed'] for v in data],[False,True,True])
        self.assertEqual(self.search_page().evaluate('[sortClicks,nextClicks]'),[1,2])
        self.assertEqual(self.summary()['total_observation_count'],9)
        self.assertEqual(self.summary()['unique_product_count'],8)
        self.assertEqual(self.summary()['stop_reason'],'requested_pages_reached')

    def test_default_one_page_does_not_click_next(self):
        """省略页数时不操作下一页。"""
        self.run_collection(pages=1)
        self.assertEqual(len(self.payloads()),1)
        self.assertEqual(self.search_page().evaluate('nextClicks'),0)

    def test_confirmed_last_page_stops_before_requested_count(self):
        """实际仅两页时按明确末页证据结束，不制造三至五页。"""
        self.body=paginated_html(total=2)
        self.run_collection(pages=5)
        self.assertEqual(len(self.payloads()),2)
        self.assertEqual(self.search_page().evaluate('nextClicks'),1)
        self.assertEqual(self.summary()['stop_reason'],'last_page')

    def test_noop_click_preserves_first_page_without_reclick(self):
        """点击未生效转人工并停止时，第一页保留且点击只有一次。"""
        self.body=paginated_html(noop=True)
        def stop(request):
            raise TaskStopped('本地核对后停止')
        with self.assertRaises(TaskStopped):
            self.run_collection(on_attention=stop)
        self.assertEqual(len(self.payloads()),1)
        self.assertEqual(self.search_page().evaluate('nextClicks'),1)
        self.assertEqual(self.summary()['completed_page_count'],1)
        self.assertEqual(self.summary()['status'],'interrupted')

    def test_sort_change_after_click_does_not_save_wrong_page(self):
        """翻页后排序变为综合时保留旧页，不误采第二页。"""
        self.body=paginated_html(reset_sort=True)
        def stop(request):
            raise TaskStopped('本地检查排序异常')
        with self.assertRaises(TaskStopped):
            self.run_collection(on_attention=stop)
        self.assertEqual([v['page_number'] for v in self.payloads()],[1])
        self.assertEqual(self.search_page().evaluate('nextClicks'),1)

    def test_storage_failure_blocks_next_page(self):
        """模拟持久化失败，后续翻页不得启动。"""
        with patch('ecommerce.modules.collection.persistence.capture_list',side_effect=NeedsAttention('本地模拟采集保存失败')):
            with self.assertRaises(NeedsAttention):
                self.run_collection()
        self.assertEqual(self.search_page().evaluate('nextClicks'),0)
        self.assertEqual(self.summary()['completed_page_count'],0)

    def test_unknown_dialog_after_next_requires_feedback_without_reclick(self):
        """翻页后未知窗口由人工关闭，再核对原目标页，不重复点击。"""
        self.body=paginated_html(total=2,dialog=True)
        calls=[]
        def attention(request):
            calls.append(request)
            self.search_page().locator('[role=dialog]').evaluate('e=>e.remove()')
            return '继续'
        self.run_collection(pages=2,on_attention=attention)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(self.payloads()),2)
        self.assertEqual(self.search_page().evaluate('nextClicks'),1)
