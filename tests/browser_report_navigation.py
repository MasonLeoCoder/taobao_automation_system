"""来源报告往返交互；临时库模拟报告，实际页面模式严格GET。"""
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
import sys
from threading import Thread
from werkzeug.serving import make_server, WSGIRequestHandler
from playwright.sync_api import sync_playwright, expect
from tests.unit import test_report_navigation as fixtures

OUTPUT=Path('workspace/output/reports/report_navigation')


def check():
    """三视口验证来源、位置、弹窗状态及免费检查；参数：无；返回：断言。"""
    OUTPUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        for width in (1440,390,320):
            context=browser.new_context(viewport={'width':width,'height':1000});page=context.new_page();errors=[];posts=[];case=None;patches=[];server=None;thread=None
            page.on('pageerror',lambda error:errors.append(str(error)))
            if '--real' not in sys.argv:
                case=fixtures.NavigationTests();case.setUp()
                for name in ('ecommerce.infrastructure.storage.collection_browser.collection_store','ecommerce.infrastructure.storage.collection_queries.collection_store'):
                    item=patch(name,return_value=SimpleNamespace(engine=case.engine,tables=case.tables));item.start();patches.append(item)
                class QuietHandler(WSGIRequestHandler):
                    def log(self,*args,**kwargs):
                        """省略正常HTTP日志，不影响断言。"""
                server=make_server('127.0.0.1',0,case.app,threaded=True,request_handler=QuietHandler)
                thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            def route(route):
                """禁止真实POST和所有外部请求；临时检查只写临时SQLite。"""
                r=route.request;url=urlsplit(r.url)
                if url.hostname!='127.0.0.1':route.abort();return
                if not case:
                    if r.method not in ('GET','HEAD'):raise AssertionError('禁止真实POST')
                    route.continue_();return
                if url.path=='/static/collection.js':route.fulfill(content_type='text/javascript',body='');return
                if url.path.startswith('/api/collections/'):
                    route.fulfill(content_type='application/json',body='{"jobs":[]}');return
                if r.method=='POST':posts.append(url.path);assert url.path.endswith('/check'),url.path
                # 原生302后续请求必须有真实本机测试服务承接，不能依赖route.fulfill拦截重定向链。
                route.continue_()
            try:
                context.route('**/*',route);base='http://127.0.0.1:'+str(server.server_port) if case else 'http://127.0.0.1:8000';product='123' if case else '1050158363561'
                panel=base+'/collections/products/'+product+'?module=composite'
                for index in (0,1):
                    response=page.goto(panel);assert response.status==200
                    source=page.locator('[data-report-source]').nth(index);source.scroll_into_view_if_needed();original_y=page.evaluate('scrollY')
                    source.click();expect(page.get_by_text('来自：综合分析',exact=True)).to_be_visible()
                    page.reload();expect(page.get_by_role('link',name='← 返回综合分析',exact=True)).to_be_visible()
                    if index==0:page.screenshot(path=str(OUTPUT/f'{"isolated" if case else "real"}-source-{width}.png'))
                    if index==1:
                        page.locator('#qa-metadata > summary').click()
                        # 明确版本链接必须继续携带返回上下文。
                        version=page.locator('#qa-metadata a').filter(has_text='版本').last
                        assert 'return_to=' in version.get_attribute('href');version.click()
                        expect(page.get_by_role('link',name='← 返回综合分析',exact=True)).to_be_visible()
                        if case:
                            page.locator('#qa-checks > summary').click();page.locator('[data-qa-check]').click()
                            expect(page.locator('#qa-checks')).to_be_visible();page.wait_for_url('**#qa-checks')
                            expect(page.get_by_role('link',name='← 返回综合分析',exact=True)).to_be_visible()
                    page.get_by_role('link',name='← 返回综合分析',exact=True).click()
                    page.wait_for_function("() => location.search.includes('module=composite') && !location.search.includes('_report_back')")
                    page.wait_for_function('(y)=>Math.abs(scrollY-y)<3',arg=original_y)
                # 弹窗选择和展开状态恢复，费用不恢复。
                page.locator('[data-ca-open]').click();expect(page.locator('[data-ca-form]')).to_be_visible(timeout=45000)
                page.locator('select[name=model] + .ss-trigger').click();page.get_by_role('option',name='deepseek-v4-pro',exact=True).click()
                expect(page.locator('[data-ca-form] [type=submit]')).to_be_enabled(timeout=45000)
                page.locator('[data-ca-advanced] > summary').click();page.locator('[name=output_tokens]').fill('4096');page.locator('[name=output_tokens]').press('Tab')
                expect(page.locator('[data-ca-form] [type=submit]')).to_be_enabled(timeout=45000)
                expect(page.locator('[name=output_tokens]')).to_have_value('4096')
                page.locator('[name=allow_paid]').check()
                selected=page.locator('[name=review_report]').input_value()
                page.locator('[data-ca-form] [data-report-source]').nth(1).click()
                expect(page.get_by_text('来自：综合分析设置',exact=True)).to_be_visible()
                page.get_by_role('link',name='← 返回综合分析设置',exact=True).click()
                expect(page.locator('#composite-analysis-dialog')).to_be_visible(timeout=45000)
                expect(page.locator('select[name=model]')).to_have_value('deepseek-v4-pro',timeout=45000)
                expect(page.locator('[name=output_tokens]')).to_have_value('4096')
                expect(page.locator('[name=review_report]')).to_have_value(selected)
                expect(page.locator('[data-ca-advanced]')).to_have_attribute('open','')
                expect(page.locator('[name=allow_paid]')).not_to_be_checked()
                page.evaluate('() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                page.wait_for_function("() => {const b=document.getElementById('composite-analysis-dialog').getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight+1}")
                page.screenshot(path=str(OUTPUT/f'{"isolated" if case else "real"}-restored-dialog-{width}.png'))
                page.locator('[data-ca-cancel]').click()
                if case:
                    # 综合报告原版本、折叠及滚动恢复。
                    page.goto(base+'/analysis/composite/tasks/'+case.composite_task+'?version=1')
                    page.locator('#ca-metadata > summary').click();link=page.locator('#ca-metadata [data-report-source]').first
                    link.scroll_into_view_if_needed();original_y=page.evaluate('scrollY');link.click()
                    expect(page.get_by_text('来自：综合报告',exact=True)).to_be_visible()
                    # 点击下载只产生附件，不离开当前页面。
                    page.locator('.ui-action-menu > summary').filter(has_text='下载报告').click();url=page.url
                    with page.expect_download():page.get_by_role('link',name='可编辑文字 · Markdown').click()
                    assert page.url==url
                    page.get_by_role('link',name='← 返回综合报告',exact=True).click()
                    page.wait_for_function("() => location.pathname.includes('/analysis/composite/tasks/') && !location.search.includes('_report_back')")
                    assert parse_qs(urlsplit(page.url).query)['version']==['1'];expect(page.locator('#ca-metadata')).to_have_attribute('open','')
                    page.wait_for_function('(y)=>Math.abs(scrollY-y)<3',arg=original_y)
                    page.screenshot(path=str(OUTPUT/f'isolated-restored-report-{width}.png'))
                    # 本标签页状态过期时返回安全页面并说明，不形成循环。
                    page.locator('#ca-metadata [data-report-source]').first.click()
                    page.evaluate('sessionStorage.clear()');page.get_by_role('link',name='← 返回综合报告',exact=True).click()
                    expect(page.get_by_text('阅读位置记录已失效，已返回原页面；请重新选择需要查看的位置。')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                assert not errors,errors;assert not posts if not case else len(posts)==1
                print('isolated' if case else 'real',width,'passed; real AI 0; POST',len(posts),flush=True)
            except Exception:
                page.screenshot(path=str(OUTPUT/f'failure-{width}.png'));print('diagnostic',page.url,errors,posts,flush=True);raise
            finally:
                context.close()
                if server:server.shutdown();thread.join(timeout=2);server.server_close()
                for item in reversed(patches):item.stop()
                if case:case.doCleanups()
        browser.close()


if __name__=='__main__':check()
