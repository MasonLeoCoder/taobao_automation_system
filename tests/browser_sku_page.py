"""SKU页面隔离与实际本机只读浏览器验收，不提交采集或AI。"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from tests.unit.test_sku_page import SkuPageTests
from tests.unit.test_image_tools import picture

OUTPUT=Path('workspace/output/reports/sku_page')


def populate(test):
    """构造多状态、长属性与跨页数据；参数：隔离测试；返回：无。"""
    test.sku(1,sku_name='替换��纸')
    test.sku(2,sku_name='粉色·1手柄＋3卷纸',properties=[{'group':'颜色','name':'粉色'},{'group':'套餐','name':'1手柄＋3卷纸'}])
    test.sku(3,stock_status='out_of_stock',price_status='out_of_stock',price_value=None,payload={'stock_evidence':'页面规格禁用：disabled属性'})
    test.sku(4,stock_status='unavailable_unconfirmed',price_status='unavailable_unconfirmed',price_value=None,payload={'price_reason':'未确认原因'})
    test.sku(5,stock_status=None,price_status='not_read',price_value=None)
    for number in range(6,29):test.sku(number)
    for number in (1,2):
        path=test.host.image(number,200+number);data=picture(size=(100,100),color=(30,120,90,255));path.write_bytes(data)
        import sqlalchemy as sa
        with test.case.engine.begin() as connection:
            f=test.case.store.tables['file_assets'];connection.execute(sa.update(f).where(f.c.id==300+number).values(size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))


def interact(page,base,product,batch,width,real):
    """检查状态、排序、原文及来源不串批；参数：页面和来源；返回：无。"""
    page.goto(f'{base}/collections/products/{product}?batch={batch}')
    root=page.locator('[data-sku-page]');expect(root).to_have_attribute('data-sku-ready','true')
    expect(root.locator('[data-sku-loaded]')).to_contain_text(f'当前批次 {batch}')
    assert root.locator('[data-sku-detail][open]').count()==0
    logs=root.locator('[data-sku-logs]').get_attribute('href')
    first=root.locator('[data-sku-row]').first
    expect(first).to_contain_text('规格文字异常')
    if real:
        expect(root.locator('[data-sku-overview]')).to_contain_text('¥3.35～¥13.80')
        expect(root.locator('[data-sku-filter="selectable"]')).to_contain_text('5')
    else:expect(root.locator('[data-sku-filter="selectable"]')).to_contain_text('25')
    root.locator('.pb-title').scroll_into_view_if_needed();page.screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-top-{width}.png'))
    root.locator('[data-sku-overview]').screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-overview-{width}.png'))
    if real:root.screenshot(path=str(OUTPUT/f'real-module-{width}.png'))
    first.screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-row-{width}.png'))
    first.locator('[data-sku-detail]>summary').click();expect(first.locator('[data-sku-detail]')).to_have_attribute('open','')
    first.locator('[data-sku-copy]').click();expect(first.locator('[data-sku-copy-feedback]')).to_contain_text('已复制')
    assert page.evaluate('navigator.clipboard.readText()')==first.locator('.ui-sku-name').inner_text()
    first.screenshot(path=str(OUTPUT/f'{"real" if real else "isolated"}-details-{width}.png'))
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    first.locator('[data-sku-detail]>summary').click();first.locator('img').click()
    expect(page.locator('#image-preview-photo')).to_be_visible();page.keyboard.press('Escape')
    expect(page.locator('#image-preview')).not_to_be_visible()
    root.locator('[data-sku-filter="out_of_stock"]').click()
    if real:
        expect(root).to_contain_text('没有符合当前状态的规格')
        expect(root.locator('[data-sku-overview]')).to_contain_text('已确认 5 / 5')
    else:
        expect(root.locator('[data-sku-row]')).to_have_count(1)
        expect(root.locator('[data-sku-row]')).to_contain_text('缺货／已禁用')
        expect(root.locator('[data-sku-overview]')).to_contain_text('已确认 25 / 28')
    root.locator('[data-sku-filter="all"]').click();root.locator('[data-sku-price-sort]').click()
    assert 'page=1' in page.url
    prices=root.locator('.ui-sku-price').all_text_contents()
    if real:assert prices==['¥3.35','¥3.58','¥4.58','¥6.88','¥13.80'],prices
    else:assert prices[:2]==['¥1.00','¥2.00'],prices
    assert root.locator('[data-sku-logs]').get_attribute('href').split('return_to=')[0]==logs.split('return_to=')[0]
    if not real:
        page.get_by_role('link',name='下一页',exact=True).click();expect(root.locator('[data-sku-row]')).to_have_count(8)
        expect(root.locator('[data-sku-overview]')).to_contain_text('已确认 25 / 28')
        root.locator('[data-sku-filter="unknown"]').click();assert 'page=1' in page.url
        expect(root.locator('[data-sku-row]')).to_have_count(1)
    root.locator('[data-sku-batch-form] .ss-trigger').click()
    if real:
        page.keyboard.press('Escape')
    else:
        page.get_by_role('option',name=re.compile('批次 60')).click()
        expect(root.locator('[data-sku-pending]')).to_contain_text('已选择批次 60')
        expect(root.locator('[data-sku-loaded]')).to_contain_text('当前批次 70')
        assert first.count()==1
        root.locator('[data-sku-batch-form] button[type=submit]').click()
        expect(root.locator('[data-sku-loaded]')).to_contain_text('当前批次 60')
        expect(root.locator('[data-sku-filter="all"]')).to_have_attribute('aria-current','true')
        expect(root.locator('[data-sku-overview]')).to_contain_text('¥99.00')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


def check(real=False):
    """在三个视口验收；参数：是否实际本机页面；返回：断言结果。"""
    OUTPUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                test=None;errors=[];blocked=[]
                if not real:
                    test=SkuPageTests();test.setUp();populate(test);c=test.case
                    c.app.config['SETTINGS']=replace(c.settings,task_backend='mysql')
                context=browser.new_context(viewport={'width':width,'height':1000},permissions=['clipboard-read','clipboard-write'])
                def respond(route):
                    """只允许本机GET及HEAD，实际模式也禁止业务写入；参数：路由；返回：响应。"""
                    r=route.request;url=urlsplit(r.url)
                    if url.hostname!='127.0.0.1' or r.method not in ('GET','HEAD'):
                        blocked.append(r.url);route.abort();return
                    if real:route.continue_();return
                    if url.path in {'/api/collection-jobs/active','/api/collections/products/123/jobs'}:
                        route.fulfill(content_type='application/json',body=json.dumps({'jobs':[],'plan':{'modules':[]},'run_status':None}));return
                    response=c.client.open(url.path+('?' +url.query if url.query else ''),method=r.method,buffered=True)
                    try:route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                    finally:response.close()
                try:
                    context.route('**/*',respond);page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    interact(page,'http://127.0.0.1:8000' if real else 'http://127.0.0.1:18765','1050158363561' if real else '123',92 if real else 70,width,real)
                    assert not errors,errors;assert not blocked,blocked
                    print('real' if real else 'isolated',width,'passed: full-batch summary, filters/sort, detail/copy, image preview and fixed batch')
                finally:
                    context.close()
                    if test:test.doCleanups()
        finally:browser.close()


if __name__=='__main__':check('--real' in sys.argv)
