"""详情图库的隔离交互及实际只读检查；只有隔离下载可POST，不采集或调用AI。"""
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile
import hashlib
import json
import sys
from PIL import Image
from playwright.sync_api import sync_playwright,expect
from tests.unit.test_detail_image_page import DetailImageTests
from tests.unit.test_image_tools import picture

OUTPUT=Path('workspace/output/reports/detail_images')


def isolated(page,context,width):
    """核对视图/长图/预览/格式记忆/下载与历史批次；参数：浏览器及视口；返回：断言。"""
    test=DetailImageTests();test.setUp();c=test.c;errors=[];posts=[]
    path=c.settings.workspace/'output/images/9001.webp';content=picture('WEBP',size=(60,1000));path.write_bytes(content)
    with c.engine.begin() as conn:
        table=c.store.tables['file_assets'];conn.execute(table.update().where(table.c.id==9001).values(size_bytes=len(content),sha256=hashlib.sha256(content).hexdigest()))
    def route(route):
        r=route.request;url=urlsplit(r.url)
        assert url.hostname=='127.0.0.1'
        if r.method=='POST':assert '/detail-batches/' in url.path;posts.append(json.loads(r.post_data))
        elif r.method not in ('GET','HEAD'):raise AssertionError('unexpected write')
        if url.path.startswith('/api/collection'):
            route.fulfill(content_type='application/json',body=json.dumps({'jobs':[],'plan':{'modules':[]}}));return
        response=c.client.open(url.path+('?' +url.query if url.query else ''),method=r.method,base_url='http://127.0.0.1:18765',
            headers={k:v for k,v in r.headers.items() if k.lower() in {'content-type','x-collection-token','origin'}},data=r.post_data_buffer,buffered=True)
        try:route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
        finally:response.close()
    try:
        context.route('**/*',route);page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto('http://127.0.0.1:18765/collections/products/123?module=detail_images')
        root=page.locator('[data-detail-image-page]');gallery=root.locator('[data-image-gallery]')
        expect(root).to_contain_text('共 24 张');expect(root.locator('[data-detail-image]')).to_have_count(20)
        root.get_by_role('button',name='连续阅读',exact=True).click();expect(gallery).to_have_attribute('data-layout','reading')
        img=gallery.locator('img').first;img.scroll_into_view_if_needed();expect(img).to_be_visible()
        assert img.evaluate('e=>Math.abs(e.getBoundingClientRect().height/e.getBoundingClientRect().width-e.naturalHeight/e.naturalWidth)<0.1')
        assert 'image_view=reading' in root.locator('.numbered-pagination a[rel=next]').get_attribute('href')
        root.get_by_role('button',name='网格浏览',exact=True).click()
        root.locator('[data-gallery-preview]').first.click();viewer=page.locator('#image-preview');expect(viewer).to_be_visible()
        expect(viewer.locator('#image-preview-title')).to_have_text('详情图片 1');viewer.get_by_role('button',name='下一张 →').click()
        expect(viewer.locator('#image-preview-title')).to_have_text('详情图片 2');viewer.get_by_role('button',name='放大图片',exact=True).click()
        viewer.locator('#image-preview-close').click()
        root.locator('[data-image-download="all"]').click();modal=page.locator('[data-image-download-modal]')
        expect(modal.locator('.ss-trigger')).to_contain_text('PNG')
        with page.expect_download() as event:modal.get_by_role('button',name='确认下载',exact=True).click()
        with ZipFile(event.value.path()) as archive:assert len(archive.namelist())==24
        assert posts[-1]=={'format':'png'}
        root.locator('[data-image-download="9001"]').click();modal.locator('.ss-trigger').click();page.get_by_role('option',name='JPG',exact=True).click()
        with page.expect_download() as event:modal.get_by_role('button',name='确认下载',exact=True).click()
        with Image.open(event.value.path()) as image:assert image.format=='JPEG'
        root.locator('[data-image-download="9001"]').click();expect(modal.locator('.ss-trigger')).to_contain_text('JPG');modal.get_by_role('button',name='取消',exact=True).click()
        before=root.locator('[data-image-logs]').get_attribute('href');form=root.locator('[data-detail-image-batch-form]');form.locator('.ss-trigger').click()
        page.get_by_role('option').filter(has_text='批次 91').click();expect(form.locator('[data-detail-image-pending]')).to_be_visible()
        assert root.locator('[data-image-logs]').get_attribute('href')==before
        form.get_by_role('button',name='查看',exact=True).click();expect(root.locator('[data-image-loaded]')).to_contain_text('当前批次 91')
        assert '/detail-batches/91/' in root.get_attribute('data-image-download-url');assert 'task_id='+('b'*32) in root.locator('[data-image-logs]').get_attribute('href')
        root.screenshot(path=str(OUTPUT/f'isolated-{width}.png'))
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');assert not errors,errors
        print(width,'isolated long aspect, reading/grid, preview next/zoom, all24 ZIP, JPG/memory, historical binding passed')
    finally:context.unroute('**/*',route);test.doCleanups()


def check():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                context=browser.new_context(viewport={'width':width,'height':1000},accept_downloads=True);page=context.new_page()
                try:
                    if '--real' not in sys.argv:isolated(page,context,width);continue
                    errors=[];writes=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    def guard(route):
                        r=route.request
                        if r.method not in ('GET','HEAD'):writes.append(r.url);route.abort()
                        elif urlsplit(r.url).hostname!='127.0.0.1':route.abort()
                        else:route.continue_()
                    context.route('**/*',guard)
                    page.goto('http://127.0.0.1:8000/collections/products/1050158363561?module=detail_images')
                    root=page.locator('[data-detail-image-page]');expect(root.locator('[data-module-update-open]')).to_be_enabled(timeout=15000)
                    expect(root).to_contain_text('共 6 张');expect(root.locator('[data-detail-image]')).to_have_count(6)
                    root.screenshot(path=str(OUTPUT/f'real-grid-{width}.png'))
                    root.get_by_role('button',name='连续阅读',exact=True).click();expect(root.locator('[data-image-gallery]')).to_have_attribute('data-layout','reading')
                    root.locator('[data-detail-image]').first.screenshot(path=str(OUTPUT/f'real-reading-{width}.png'))
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');assert not errors,errors;assert not writes
                    print(width,'actual six detail images grid/reading read-only passed; POST 0')
                finally:context.close()
        finally:browser.close()


if __name__=='__main__':check()
