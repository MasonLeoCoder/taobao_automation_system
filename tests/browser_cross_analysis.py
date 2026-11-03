"""交叉分析隔离及真实只读页面验收；所有POST和外站请求均被阻断。"""
import json
import sys
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from werkzeug.serving import make_server, WSGIRequestHandler
from playwright.sync_api import sync_playwright, expect
from tests.unit.test_cross_analysis import CrossTests

OUTPUT = Path('workspace/output/reports/cross_analysis')


def check():
    """检查四类选择、只读重规划、往返和三种视口，不点击付费开始。"""
    OUTPUT.mkdir(parents=True,exist_ok=True); real='--real' in sys.argv; case=None; server=None
    if not real:
        case=CrossTests();case.setUp()
        for target in ('ecommerce.infrastructure.storage.collection_browser.collection_store','ecommerce.infrastructure.storage.collection_queries.collection_store'):
            current=patch(target,return_value=SimpleNamespace(engine=case.engine,tables=case.tables));current.start();case.addCleanup(current.stop)
        task=case.start()['id'];case.execute(task)
        class Quiet(WSGIRequestHandler):
            def log(self,*args,**kwargs):
                """不输出普通页面请求日志。"""
        server=make_server('127.0.0.1',0,case.app,threaded=True,request_handler=Quiet);Thread(target=server.serve_forever,daemon=True).start()
    base='http://127.0.0.1:'+str(server.server_port if server else 8000); product=('994238715275' if '--four' in sys.argv else '1050158363561') if real else '123'; results=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            for width in (1440,390,320):
                context=browser.new_context(viewport=dict(width=width,height=1000));page=context.new_page();errors=[];blocked=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def guard(route):
                    """外站及变更请求禁止；临时环境停用正式队列轮询。"""
                    request=route.request; parsed=urlsplit(request.url)
                    if request.method not in ('GET','HEAD') or parsed.hostname!='127.0.0.1':
                        blocked.append(request.method+' '+parsed.path);route.abort();return
                    if case and parsed.path=='/static/collection.js':route.fulfill(content_type='text/javascript',body='');return
                    route.continue_()
                context.route('**/*',guard)
                try:
                    response=page.goto(base+f'/collections/products/{product}?module=composite');assert response.status==200
                    expect(page.get_by_role('heading',name='交叉分析',exact=True)).to_be_visible()
                    assert page.locator('[data-cross-picker] [data-cross-row]').count()==4
                    page.wait_for_function('() => document.documentElement.scrollWidth <= innerWidth+2')
                    page.locator('[data-cross-picker]').scroll_into_view_if_needed();page.screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-picker-{width}.png'),full_page=True)
                    page.get_by_role('button',name='生成交叉报告',exact=True).click()
                    form=page.locator('[data-ca-form]');expect(form).to_be_visible(timeout=60000)
                    expect(form.locator('[name=allow_paid]')).not_to_be_checked()
                    # 连续切换组合，低于两类仍可操作，不发送请求；每次重规划清除授权。
                    available=[kind for kind in ('reviews','questions','main_images','detail_images') if form.locator('[name=use_'+kind+']').is_enabled()]
                    if len(available)>2:
                        form.locator('[name=allow_paid]').check()
                        form.locator('[name=use_'+available[-1]+']').uncheck()
                        expect(form.locator('[name=allow_paid]')).not_to_be_checked(timeout=60000)
                        expect(form.locator('[type=submit]')).to_be_enabled(timeout=60000)
                        expect(form.locator('[name=use_'+available[-1]+']')).not_to_be_checked()
                    form.locator('[data-ca-advanced]>summary').click();form.locator('[name=output_tokens]').fill('4096');form.locator('[name=output_tokens]').press('Tab')
                    expect(form.locator('[type=submit]')).to_be_enabled(timeout=60000)
                    expect(form.locator('[name=output_tokens]')).to_have_value('4096');expect(form.locator('[name=allow_paid]')).not_to_be_checked()
                    page.wait_for_function('() => {const b=document.querySelector("#composite-analysis-dialog").getBoundingClientRect();return b.left>=0&&b.right<=innerWidth+1&&b.top>=0&&b.bottom<=innerHeight+1}')
                    page.screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-dialog-{width}.png'))
                    # 来源往返恢复设置弹窗，费用勾选不得恢复。
                    link=form.locator('[data-cross-view]').first;link.click()
                    expect(page.get_by_role('link',name='← 返回交叉分析设置',exact=True)).to_be_visible()
                    page.get_by_role('link',name='← 返回交叉分析设置',exact=True).click()
                    expect(page.locator('[data-ca-form]')).to_be_visible(timeout=60000)
                    expect(page.locator('[data-ca-form] [name=output_tokens]')).to_have_value('4096')
                    expect(page.locator('[data-ca-form] [name=allow_paid]')).not_to_be_checked()
                    page.locator('[data-ca-cancel]').click()
                    if case:
                        page.goto(base+'/analysis/composite/tasks/'+task)
                        expect(page.get_by_role('heading',name='交叉选品报告',exact=True)).to_be_visible()
                        page.locator('#ca-metadata>summary').click()
                        for kind in ('reviews','questions','main_images','detail_images'):
                            link=page.locator('#ca-metadata [data-report-source]').nth(list(('reviews','questions','main_images','detail_images')).index(kind));link.click()
                            expect(page.get_by_role('link',name='← 返回交叉报告',exact=True)).to_be_visible()
                            page.get_by_role('link',name='← 返回交叉报告',exact=True).click()
                            expect(page.get_by_role('heading',name='交叉选品报告',exact=True)).to_be_visible()
                        page.screenshot(path=str(OUTPUT/f'isolated-report-{width}.png'))
                        page.wait_for_function('() => document.documentElement.scrollWidth <= innerWidth+2')
                    assert not errors,errors;assert not blocked,blocked
                    results.append(dict(width=width,mode='real-four' if real and '--four' in sys.argv else 'real' if real else 'isolated',errors=errors,blocked=blocked))
                    print('real-four' if real and '--four' in sys.argv else 'real' if real else 'isolated',width,'selection/dialog/return checks passed')
                finally:context.close()
            browser.close()
    finally:
        if server:server.shutdown()
        if case:case.doCleanups()
    (OUTPUT/('real-four-browser.json' if real and '--four' in sys.argv else 'real-browser.json' if real else 'isolated-browser.json')).write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':check()
