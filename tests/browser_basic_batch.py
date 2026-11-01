"""基本信息批次卡片三视口回归，全部数据来自临时样例。"""
from dataclasses import replace
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright,expect
from tests.unit import test_basic_batch as fixtures


def check():
    """核对历史来源、待查看、中文搜索、异常和无脚本回退；参数：无；返回：断言。"""
    output=Path('workspace/output/reports/basic_batch_layout/isolated');output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                test=fixtures.BasicBatchTests();test.setUp();c=test.case;errors=[];blocked=[];documents=[]
                c.app.config['SETTINGS']=replace(c.settings,task_backend='mysql')
                context=browser.new_context(viewport={'width':width,'height':1000})
                def respond(route):
                    """只提供本地GET页面或模拟只读进度；参数：路由；返回：响应。"""
                    r=route.request;url=urlsplit(r.url)
                    if r.method!='GET' or url.hostname!='127.0.0.1':blocked.append(r.method);route.abort();return
                    if r.is_navigation_request():documents.append(url.path+'?'+url.query)
                    payload=None
                    if url.path=='/api/collections/products/123/jobs':payload={'jobs':[],'plan':{'modules':[]},'run_status':None}
                    elif url.path=='/api/collection-jobs/active':payload={'jobs':[]}
                    if payload is not None:route.fulfill(content_type='application/json',body=json.dumps(payload));return
                    response=c.client.get(url.path+('?' +url.query if url.query else ''))
                    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                try:
                    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.route('**/*',respond)
                    page.goto('http://127.0.0.1:8000/collections/products/123?batch=60')
                    root=page.locator('[data-basic-batch]')
                    expect(root.locator('[data-bb-price]')).to_have_text('旧批价格 11元')
                    expect(root.locator('[data-bb-loaded]')).to_contain_text('历史批次')
                    expect(root.locator('.ss-trigger')).to_contain_text('批次 60')
                    logs=root.locator('[data-bb-logs]').get_attribute('href');task=root.locator('[data-bb-task]').get_attribute('href')
                    assert ('b'*32) in logs and ('b'*32) in task
                    initial=len(documents)
                    root.locator('.ss-trigger').click();page.get_by_role('combobox',name='查找采集批次',exact=True).fill('70')
                    page.get_by_role('option',name=re.compile('批次 70')).click()
                    expect(root.locator('[data-bb-pending]')).to_contain_text('已选择批次 70')
                    expect(root.locator('[data-bb-price]')).to_have_text('旧批价格 11元')
                    assert len(documents)==initial
                    assert root.locator('[data-bb-logs]').get_attribute('href')==logs
                    assert root.locator('[data-bb-task]').get_attribute('href')==task
                    root.locator('[data-bb-form] button[type=submit]').click()
                    expect(root.locator('[data-bb-loaded]')).to_contain_text('最近有效批次')
                    expect(root.locator('[data-bb-price]')).to_have_text('当前价格 22元')
                    expect(root.locator('[data-bb-pending]')).to_be_hidden()
                    assert ('a'*32) in root.locator('[data-bb-logs]').get_attribute('href')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    assert root.locator('table').count()==0
                    root.screenshot(path=str(output/f'batch-{width}.png'))
                    assert not root.locator('[data-bb-source]').evaluate('(node)=>node.open')
                    root.locator('[data-bb-source]>summary').click();expect(root).to_contain_text('来源任务：'+('a'*32))
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.goto('http://127.0.0.1:8000/collections/products/123?batch=80')
                    expect(root.locator('.pb-notice')).to_contain_text('字段读取未完成')
                    expect(root.locator('[data-bb-price]')).to_have_text('部分价格 33元')
                    assert root.locator('script').count()==0
                    root.get_by_role('link',name='返回基本信息',exact=True).click();expect(page.locator('.pd-basic')).to_contain_text('当前价格 22元')
                    if width==1440:
                        plain=browser.new_context(java_script_enabled=False);raw=plain.new_page();raw.route('**/*',respond)
                        try:
                            raw.goto('http://127.0.0.1:8000/collections/products/123?module=basic_info')
                            raw.locator('[data-bb-select]').select_option('60');raw.locator('[data-bb-form] button[type=submit]').click()
                            expect(raw.locator('[data-bb-price]')).to_have_text('旧批价格 11元')
                        finally:plain.close()
                    assert not errors,errors;assert not blocked,blocked
                    print(width,'passed: bound sources, pending selection, explicit GET switch, status, no overflow, no writes')
                finally:context.close();test.doCleanups()
        finally:browser.close()


if __name__=='__main__':check()
