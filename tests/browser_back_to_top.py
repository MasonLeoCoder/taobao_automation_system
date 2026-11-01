"""全站回顶部交互验证；隔离页面和实际本机只读页面均不允许POST。"""
from pathlib import Path
from urllib.parse import urlsplit
import json
import sys
from playwright.sync_api import sync_playwright,expect
from tests.unit import test_collection_browser as fixtures

OUTPUT=Path('workspace/output/reports/back_to_top')


def interactions(page,width):
    """验证阈值、动画、遮挡、键盘和偏好；参数：页面和宽度；返回：无。"""
    button=page.locator('#back-to-top');expect(button).to_have_count(1)
    page.evaluate('scrollTo(0,0)');expect(button).to_be_hidden()
    page.evaluate("document.querySelector('main').append(Object.assign(document.createElement('div'),{id:'test-scroll-space',style:'height:6000px'}))")
    page.evaluate('scrollTo(0,450)');expect(button).to_be_hidden()
    page.evaluate('scrollTo(0,900)');expect(button).to_be_visible()
    assert button.bounding_box()['height']>=44
    if width<=700:expect(button.locator('.ui-back-to-top-label')).to_be_hidden()
    page.screenshot(path=str(OUTPUT/f'visible-{width}.png'))
    url=page.url;button.click()
    try:page.wait_for_function('() => scrollY<1')
    except Exception:
        print('scroll diagnostic',page.evaluate("({y:scrollY,active:document.activeElement.id,hidden:document.getElementById('back-to-top').hidden})"));raise
    expect(button).to_be_hidden();assert page.url==url
    page.evaluate('scrollTo(0,900)');expect(button).to_be_visible()
    page.evaluate("const d=document.createElement('dialog');d.id='backtop-test-modal';d.textContent='验证弹窗';document.body.append(d);d.showModal()")
    expect(button).to_be_hidden();page.evaluate("document.getElementById('backtop-test-modal').close()")
    expect(button).to_be_visible()
    page.evaluate("const p=document.createElement('div');p.id='backtop-test-popover';p.popover='auto';p.textContent='验证浮层';document.body.append(p);p.showPopover()")
    expect(button).to_be_hidden();page.evaluate("document.getElementById('backtop-test-popover').hidePopover()")
    expect(button).to_be_visible()
    page.evaluate("const b=document.getElementById('collection-task-bar');b.textContent='采集任务状态占位';b.style.height='120px';b.hidden=false")
    page.wait_for_function("() => document.getElementById('back-to-top').getBoundingClientRect().bottom < document.getElementById('collection-task-bar').getBoundingClientRect().top")
    page.evaluate("document.getElementById('collection-task-bar').style.height='200px'")
    page.wait_for_function("() => document.getElementById('back-to-top').getBoundingClientRect().bottom < document.getElementById('collection-task-bar').getBoundingClientRect().top")
    page.screenshot(path=str(OUTPUT/f'task-bar-{width}.png'))
    page.evaluate("document.getElementById('collection-task-bar').hidden=true")
    button.focus();page.keyboard.press('Enter');page.wait_for_function('() => scrollY<1')
    assert page.evaluate("document.activeElement.id")=='main'
    page.emulate_media(reduced_motion='reduce')
    page.evaluate('scrollTo(0,900)');expect(button).to_be_visible();button.click();page.wait_for_function('() => scrollY<1')
    assert page.url==url;assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


def check():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                context=browser.new_context(viewport={'width':width,'height':1000});page=context.new_page();errors=[];writes=[]
                page.on('pageerror',lambda error:(errors.append(str(error)),print('page error',str(error))))
                case=None
                if '--real' not in sys.argv:case=fixtures.CollectionBrowserTests();case.setUp()
                def route(route):
                    r=route.request;url=urlsplit(r.url)
                    if r.method not in ('GET','HEAD'):writes.append(r.url);route.abort();return
                    if url.hostname!='127.0.0.1':route.abort();return
                    # 仅隔离交互中停用采集轮询，避免人工状态栏占位被原轮询清空。
                    if case and url.path=='/static/collection.js':route.fulfill(content_type='text/javascript',body='');return
                    if not case:route.continue_();return
                    response=case.client.open(url.path+('?' +url.query if url.query else ''),method=r.method)
                    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                try:
                    context.route('**/*',route)
                    base='http://127.0.0.1:8000' if not case else 'http://127.0.0.1:18765'
                    paths=('/tools','/tools/images','/tasks','/logs','/modules/collection','/settings/ai',
                        '/collections/products/'+('1050158363561' if not case else '123')+'?module=parameters')
                    for path in paths:
                        response=page.goto(base+path);assert response.status==200,(path,response.status)
                        expect(page.locator('#back-to-top')).to_have_count(1)
                        assert page.locator('script[src$="back_to_top.js"]').count()==1
                    if case:
                        page.goto(base+'/tools');interactions(page,width)
                    else:
                        page.goto(base+'/collections/products/1050158363561?module=detail_images&image_view=reading')
                        page.wait_for_function("() => [...document.querySelectorAll('[data-gallery-image]')].some(img=>img.naturalHeight>0)")
                        if page.locator('dialog[open]').count():
                            expect(page.locator('#back-to-top')).to_be_hidden()
                            print('real',width,'active collection dialog correctly hides back-to-top; no task interaction')
                            assert not errors,errors;assert not writes,writes
                            continue
                        page.evaluate('scrollTo(0,1500)');expect(page.locator('#back-to-top')).to_be_visible()
                        page.locator('#back-to-top').click();page.wait_for_function('() => scrollY<1')
                    assert not errors,errors;assert not writes,writes
                    print('real' if not case else 'isolated',width,'shared pages and back-to-top checks passed; POST 0')
                finally:
                    context.close()
                    if case:case.doCleanups()
        finally:browser.close()


if __name__=='__main__':check()
