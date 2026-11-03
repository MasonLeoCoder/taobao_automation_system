"""主图/详情模块的三视口浏览器检查；真实页面严格GET，不触发AI。"""
from pathlib import Path
from threading import Thread
from unittest.mock import Mock
from urllib.parse import urlsplit
import sys
from werkzeug.serving import make_server,WSGIRequestHandler
from playwright.sync_api import sync_playwright,expect
from tests.unit import test_independent_image_analysis as fixtures

OUTPUT=Path('workspace/output/reports/separate_image_analysis')


def check():
    """验证各自入口、批次、模态框、历史、报告和图片定位。"""
    OUTPUT.mkdir(parents=True,exist_ok=True); real='--real' in sys.argv
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        for width in (1440,390,320):
            case=None;server=None;tasks={};errors=[];blocked=[]
            if not real:
                case=fixtures.IndependentImageTests();case.setUp()
                for module in ('main_images','detail_images'):tasks[module]=case.run_mock(module)[0]
                class Quiet(WSGIRequestHandler):
                    def log(self,*args,**kwargs):
                        """省略正常请求的资料路径日志。"""
                server=make_server('127.0.0.1',0,case.app,threaded=True,request_handler=Quiet);Thread(target=server.serve_forever,daemon=True).start()
            context=browser.new_context(viewport={'width':width,'height':1000});page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            def route(value):
                """阻断外站与POST，隔离样例停用队列轮询。"""
                url=urlsplit(value.request.url)
                if value.request.method not in ('GET','HEAD') or url.hostname!='127.0.0.1':
                    blocked.append(value.request.method+' '+url.path);value.abort();return
                if case and url.path=='/static/collection.js':value.fulfill(content_type='text/javascript',body='');return
                if case and url.path.startswith('/api/collections/'):value.fulfill(content_type='application/json',body='{"jobs":[]}');return
                value.continue_()
            context.route('**/*',route);base='http://127.0.0.1:'+str(server.server_port if server else 8000);product='123' if case else '1050158363561'
            try:
                for module,label,batch in (('main_images','主图',7),('detail_images','详情图',8)):
                    url=base+f'/collections/products/{product}?module={module}'+(f'&batch={batch}' if case else '')
                    response=page.goto(url);assert response.status==200
                    panel=page.locator('.ia-module-analysis');expect(panel).to_be_visible()
                    page.locator('[data-ia-open]').scroll_into_view_if_needed()
                    page.screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-{module}-{width}.png'),full_page=True)
                    page.locator('[data-ia-open]').click();expect(page.locator('[data-ia-form]')).to_be_visible(timeout=60000)
                    expect(page.locator('#ia-dialog-title')).to_have_text(label+'分析')
                    assert page.locator('select[name=batch_id]').count()==1
                    assert page.locator('[name=main_batch],[name=detail_batch]').count()==0
                    expect(page.locator('[name=allow_paid]')).not_to_be_checked()
                    for _ in range(2):
                        page.locator('select[name=model] + .ss-trigger').click();expect(page.get_by_role('option',name='deepseek-flash',exact=True)).to_be_visible();page.keyboard.press('Escape')
                    page.locator('[name=allow_paid]').check();page.locator('[data-ia-advanced]>summary').click()
                    page.locator('[name=output_tokens]').fill('4096');page.locator('[name=output_tokens]').press('Tab')
                    expect(page.locator('[data-ia-form] [type=submit]')).to_be_enabled(timeout=60000)
                    expect(page.locator('[name=allow_paid]')).not_to_be_checked();expect(page.locator('[name=output_tokens]')).to_have_value('4096')
                    # 切片预览只有本模块图号，跨分页范围由服务端整批查询保障。
                    page.locator('[data-ia-form] .ia-preview-grid details').first.locator('summary').click()
                    preview=page.locator('[data-ia-form] .ia-preview-grid img').first;expect(preview).to_be_visible()
                    page.wait_for_function('() => {const i=document.querySelector("[data-ia-form] .ia-preview-grid img");return i.complete&&i.naturalWidth>0}')
                    page.wait_for_function('() => {const b=document.getElementById("image-analysis-dialog").getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight+1}')
                    page.screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-{module}-dialog-{width}.png'))
                    page.locator('[data-ia-cancel]').click()
                    panel.get_by_role('link',name='查看分析记录',exact=True).click();expect(page.get_by_role('heading',name=label+'分析记录',exact=True)).to_be_visible()
                    assert f'/analysis/images/{module}/products/' in page.url
                    page.wait_for_function('() => document.documentElement.scrollWidth<=innerWidth+2')
                    page.screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-{module}-history-{width}.png'),full_page=True)
                    if case:
                        assert page.locator('tbody tr').count()==1
                        page.get_by_role('link',name='查看报告',exact=True).last.click();expect(page.get_by_role('heading',name=label+'分析报告',exact=True)).to_be_visible()
                        marker='【主图1】' if module=='main_images' else '【详情1】'
                        page.get_by_role('link',name=marker,exact=True).click();source=page.locator('.ia-source-fold').first
                        expect(source).to_have_attribute('open','');expect(source.locator('img')).to_be_visible()
                        page.wait_for_function('() => {const i=document.querySelector(".ia-source-fold img");return i.complete&&i.naturalWidth>0}')
                        page.screenshot(path=str(OUTPUT/f'isolated-{module}-report-{width}.png'))
                        page.get_by_role('link',name='返回'+label+'模块',exact=True).click();assert f'batch={batch}' in page.url
                        expect(page.locator('.ia-module-analysis').get_by_role('link',name='查看本批报告')).to_be_visible()
                    page.wait_for_function('() => document.documentElement.scrollWidth<=innerWidth+2')
                    assert not errors,errors;assert not blocked,blocked
                    print('real' if real else 'isolated',module,width,'GET-only module/dialog/history/report checks passed')
            finally:
                context.close()
                if server:server.shutdown()
                if case:case.doCleanups()
        browser.close()


if __name__=='__main__':check()
