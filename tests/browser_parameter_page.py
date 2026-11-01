"""参数页隔离交互及实际只读布局检查，禁止正式提交。"""
from pathlib import Path
from urllib.parse import urlsplit
import json
import sys
from playwright.sync_api import sync_playwright,expect
from tests.unit.test_parameter_page import ParameterPageTests

OUTPUT=Path('workspace/output/reports/parameter_page')


def isolated(page,context,width):
    """验证待查看批次、长值和手机排列；参数：浏览器及视口；返回：断言。"""
    test=ParameterPageTests();test.setUp();c=test.case;errors=[];writes=[]
    value='第一行\n'+'颜色与型号♥ '*100+'\n末尾  '
    c.put('parameter_observations',id=22,batch_id=20,position=3,name='颜色分类',value=value)
    for n in range(21):c.put('parameter_observations',id=300+n,batch_id=20,position=4+n,name='重复名称',value=str(n))
    c.batch(200,'parameters',product_id=1,task_id='b'*32)
    c.put('parameter_observations',id=500,batch_id=200,position=1,name='型号',value='20撕')
    def route(route):
        r=route.request;url=urlsplit(r.url)
        if r.method not in ('GET','HEAD'):writes.append(r.url);route.abort();return
        if url.hostname!='127.0.0.1':route.abort();return
        if url.path.startswith('/api/'):
            route.fulfill(content_type='application/json',body=json.dumps({'jobs':[],'plan':{'modules':[]}}));return
        response=c.client.open(url.path+('?' +url.query if url.query else ''),method=r.method)
        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
    try:
        context.route('**/*',route);page.on('pageerror',lambda e:errors.append(str(e)))
        context.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.parameterCopied=text}}})")
        page.goto('http://127.0.0.1:18765/collections/products/123?module=parameters')
        root=page.locator('[data-parameter-page]');expect(root.locator('[data-parameter-total]')).to_have_text('共 24 项')
        expect(root.locator('[data-parameter-row]')).to_have_count(20)
        valueNode=page.locator('#parameter-value-22');expect(valueNode).to_have_text(value.strip())
        expand=valueNode.locator('..').locator('.parameter-toggle')
        expect(expand).to_be_visible();expand.click();expect(expand).to_have_text('收起')
        valueNode.click();assert page.evaluate('window.parameterCopied')==value
        logs=root.locator('[data-parameter-logs]').get_attribute('href')
        form=root.locator('[data-parameter-batch-form]');form.locator('.ss-trigger').click()
        page.get_by_role('option').filter(has_text='批次 200').click()
        expect(form.locator('[data-parameter-pending]')).to_be_visible()
        expect(root.locator('[data-parameter-loaded]')).to_contain_text('批次 20')
        assert root.locator('[data-parameter-logs]').get_attribute('href')==logs
        form.get_by_role('button',name='查看',exact=True).click()
        expect(root.locator('[data-parameter-loaded]')).to_contain_text('批次 200')
        expect(root).to_contain_text('20撕');expect(root.locator('[data-parameter-total]')).to_have_text('共 1 项')
        assert '/tasks/'+('b'*32) in root.locator('[data-parameter-task]').get_attribute('href')
        expect(root.locator('[data-parameter-source]')).not_to_have_attribute('open','')
        if width<=600:assert root.locator('.pp-table tbody tr').first.evaluate("e=>getComputedStyle(e).display")=='grid'
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');assert not writes;assert not errors,errors
        root.screenshot(path=str(OUTPUT/f'isolated-history-{width}.png'))
        print(width,'isolated: full-batch count, long expand/copy, pending selection preserves source, explicit switch, mobile layout passed')
    finally:context.unroute('**/*',route);test.doCleanups()


def check():
    """三个屏宽验证；参数由--real选择只读实际商品；返回：断言。"""
    OUTPUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                context=browser.new_context(viewport={'width':width,'height':1000});page=context.new_page()
                try:
                    if '--real' not in sys.argv:isolated(page,context,width);continue
                    writes=[];errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    def guard(route):
                        r=route.request
                        if r.method not in ('GET','HEAD'):writes.append(r.url);route.abort()
                        elif urlsplit(r.url).hostname!='127.0.0.1':route.abort()
                        else:route.continue_()
                    context.route('**/*',guard)
                    page.goto('http://127.0.0.1:8000/collections/products/1050158363561?module=parameters')
                    root=page.locator('[data-parameter-page]');expect(root.locator('[data-module-update-open]')).to_be_enabled(timeout=15000)
                    expect(root.locator('[data-parameter-total]')).to_have_text('共 3 项')
                    expect(root.locator('[data-parameter-row]')).to_have_count(3)
                    expect(root.locator('[data-parameter-source]')).not_to_have_attribute('open','')
                    root.screenshot(path=str(OUTPUT/f'real-{width}.png'))
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');assert not writes;assert not errors,errors
                    print(width,'actual parameter page read-only passed; POST 0')
                finally:context.close()
        finally:browser.close()


if __name__=='__main__':check()
