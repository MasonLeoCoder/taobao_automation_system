"""图片分析三视口交互：隔离样例及实际GET，阻断外站和真实付费提交。"""
import sys
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock,patch
from urllib.parse import urlsplit
from werkzeug.serving import make_server,WSGIRequestHandler
from playwright.sync_api import sync_playwright,expect
from tests.unit.test_image_analysis import ImageAnalysisTests,sample
from ecommerce.modules.image_analysis import runtime

OUTPUT=Path('workspace/output/reports/image_analysis')


def check():
    """检查来源、费用清除、模态框边界、图片定位与可读布局。"""
    OUTPUT.mkdir(parents=True,exist_ok=True); real='--real' in sys.argv
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        for width in (1440,390,320):
            case=None; patches=[]; server=None; errors=[]; requests=[]
            if not real:
                case=ImageAnalysisTests();case.setUp(); task=case.start()
                client=Mock();client.complete_images.return_value=dict(text=sample(),finish_reason='stop',usage={'total_tokens':300})
                runtime.execute(case.settings,task,Mock(),Mock(return_value=client))
                for name in ('ecommerce.infrastructure.storage.collection_browser.collection_store','ecommerce.infrastructure.storage.collection_queries.collection_store'):
                    item=patch(name,return_value=SimpleNamespace(engine=case.engine,tables=case.tables));item.start();patches.append(item)
                class Quiet(WSGIRequestHandler):
                    def log(self,*args,**kwargs):
                        """正常请求日志不打印原文或路径。"""
                server=make_server('127.0.0.1',0,case.app,threaded=True,request_handler=Quiet);Thread(target=server.serve_forever,daemon=True).start()
            context=browser.new_context(viewport={'width':width,'height':1000});page=context.new_page()
            def route(value):
                """禁止POST和外站，仅测试本机GET。"""
                url=urlsplit(value.request.url)
                if value.request.method not in ('GET','HEAD') or url.hostname!='127.0.0.1':
                    requests.append(value.request.method+' '+url.path);value.abort();return
                if not real and url.path=='/static/collection.js':value.fulfill(content_type='text/javascript',body='');return
                if not real and url.path.startswith('/api/collections/'):value.fulfill(content_type='application/json',body='{"jobs":[]}');return
                value.continue_()
            context.route('**/*',route);page.on('pageerror',lambda e:errors.append(str(e)))
            base='http://127.0.0.1:'+str(server.server_port if server else 8000); product='123' if case else '1050158363561'
            try:
                response=page.goto(base+f'/collections/products/{product}?module=composite&analysis=images');assert response.status==200
                expect(page.get_by_role('heading',name='主图与详情分析',exact=True)).to_be_visible()
                page.screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-workspace-{width}.png'),full_page=True)
                page.locator('[data-report-source]').first.click()
                expect(page.get_by_role('link',name='← 返回图片分析',exact=True)).to_be_visible()
                page.get_by_role('link',name='← 返回图片分析',exact=True).click()
                page.wait_for_function("() => location.search.includes('analysis=images') && !location.search.includes('_report_back')")
                page.locator('[data-ia-open]').click();expect(page.locator('[data-ia-form]')).to_be_visible(timeout=60000)
                expect(page.locator('[name=allow_paid]')).not_to_be_checked()
                page.wait_for_function('() => {const b=document.getElementById("image-analysis-dialog").getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight+1}')
                page.screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-dialog-{width}.png'))
                assert page.locator('select[name=model] option').all_text_contents()==['deepseek-flash']
                for _ in range(2):
                    page.locator('select[name=model] + .ss-trigger').click();expect(page.get_by_role('option',name='deepseek-flash',exact=True)).to_be_visible();page.keyboard.press('Escape')
                page.locator('[name=allow_paid]').check()
                page.locator('[data-ia-advanced] > summary').click();page.locator('[name=output_tokens]').fill('4096');page.locator('[name=output_tokens]').press('Tab')
                expect(page.locator('[name=output_tokens]')).to_have_value('4096');expect(page.locator('[name=allow_paid]')).not_to_be_checked(timeout=60000)
                expect(page.locator('[data-ia-form] [type=submit]')).to_be_enabled(timeout=60000)
                page.locator('[data-ia-cancel]').click();expect(page.locator('#image-analysis-dialog')).not_to_be_visible()
                if case:
                    page.goto(base+'/analysis/images/tasks/'+task);expect(page.get_by_role('heading',name='主图与详情选品报告')).to_be_visible()
                    page.get_by_role('link',name='【主图1】',exact=True).click();expect(page.locator('#image-source-30')).to_have_attribute('open','')
                    expect(page.locator('#ia-sources')).to_have_attribute('open','');expect(page.locator('#image-source-30 img')).to_be_visible()
                    assert page.locator('#image-source-30 img').evaluate('(image)=>image.complete&&image.naturalWidth>0')
                    page.screenshot(path=str(OUTPUT/f'isolated-image-source-{width}.png'))
                    page.get_by_role('link',name='返回图片分析',exact=True).click();assert 'analysis=images' in page.url
                page.wait_for_function('() => document.documentElement.scrollWidth<=innerWidth+2')
                assert not errors,errors;assert not requests,requests
                print(('real' if real else 'isolated'),width,'GET-only layout/dialog/capacity controls passed')
            finally:
                context.close()
                if server:server.shutdown()
                for item in patches:item.stop()
                if case:case.doCleanups()
        browser.close()


if __name__=='__main__':check()
