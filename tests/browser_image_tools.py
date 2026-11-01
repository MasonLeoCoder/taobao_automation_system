"""工具箱及主图画廊的浏览器验证；真实模式仅转换合成上传或读取已有主图。"""
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from zipfile import ZipFile

from PIL import Image
from playwright.sync_api import sync_playwright, expect
from tests.unit.test_image_tools import ImageToolPageTests, picture

OUTPUT=Path('workspace/output/reports/image_tools')


def decoded_download(download, expected):
    """核对浏览器实际下载字节；参数：下载对象和格式；返回：文件名。"""
    path=download.path();data=Path(path).read_bytes()
    if expected=='zip':
        with ZipFile(BytesIO(data)) as archive:
            assert archive.namelist()
            for name in archive.namelist():
                with Image.open(BytesIO(archive.read(name))) as image:assert image.format=='PNG'
    else:
        with Image.open(BytesIO(data)) as image:assert image.format==expected
    return download.suggested_filename


def tool_check(page, root_url):
    """检查上传、格式记忆、重复下载和失败提示；参数：页面和地址；返回：无。"""
    page.goto(root_url+'/tools');page.get_by_role('link',name='打开工具 →').click()
    root=page.locator('[data-image-converter]');expect(root.locator('.ss-trigger')).to_contain_text('PNG')
    expect(root).to_have_attribute('data-image-ready','true')
    root.locator('[data-image-files]').set_input_files([
        {'name':'相同.webp','mimeType':'image/webp','buffer':picture('WEBP')},
        {'name':'相同.jpg','mimeType':'image/jpeg','buffer':picture('JPEG')},
    ])
    expect(root.locator('[data-image-count]')).to_have_text('2 张')
    with page.expect_download() as event:root.locator('[data-image-convert]').click()
    decoded_download(event.value,'zip');expect(root.locator('[data-image-feedback]')).to_contain_text('已准备 2 张')
    expect(root.locator('[data-image-list] li')).to_have_count(2)
    root.locator('[data-image-list] li').last.get_by_role('button',name=re.compile('移除')).click()
    root.locator('.ss-trigger').click();page.get_by_role('option',name='JPG',exact=True).click()
    with page.expect_download() as event:root.locator('[data-image-convert]').click()
    assert decoded_download(event.value,'JPEG').endswith('.jpg')
    page.reload();expect(root.locator('.ss-trigger')).to_contain_text('JPG');expect(root.locator('[data-image-count]')).to_have_text('0 张')
    root.locator('[data-image-files]').set_input_files({'name':'损坏.png','mimeType':'image/png','buffer':b'broken'})
    root.locator('[data-image-convert]').click();expect(root.locator('[data-image-feedback]')).to_contain_text('损坏.png')
    expect(root.locator('[data-image-feedback]')).to_have_class(re.compile('is-error'));expect(root.locator('[data-image-list] li')).to_have_count(1)
    root.locator('[data-image-clear]').click();expect(root.locator('[data-image-count]')).to_have_text('0 张')
    root.locator('[data-image-drop]').evaluate("""(zone,bytes)=>{
        const transfer=new DataTransfer();transfer.items.add(new File([new Uint8Array(bytes)],'拖入.png',{type:'image/png'}));
        zone.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer}));
    }""",list(picture()))
    expect(root.locator('[data-image-count]')).to_have_text('1 张')
    root.locator('[data-image-clear]').click();expect(root.locator('[data-image-count]')).to_have_text('0 张')


def gallery_check(page,root_url,product,batch,old_batch=None):
    """检查画廊、预览和格式弹窗；参数：页面、地址、商品、批次和可选旧批；返回：无。"""
    page.goto(f'{root_url}/collections/products/{product}?batch={batch}')
    root=page.locator('[data-main-image-page]');expect(root.locator('[data-image-loaded]')).to_contain_text(f'当前批次 {batch}')
    count=root.locator('[data-main-image]').count();assert count>1
    expect(root.locator('[data-image-source]')).not_to_have_attribute('open','')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    root.screenshot(path=str(OUTPUT/f'main-{product}-{page.viewport_size["width"]}.png'))
    root.locator('[data-gallery-preview]').first.click();viewer=page.locator('#image-preview');expect(viewer).to_be_visible()
    expect(viewer.locator('#image-preview-photo')).to_be_visible()
    controls=viewer.locator('.image-preview-gallery-controls')
    assert controls.evaluate('(element)=>element.scrollWidth<=element.clientWidth+1')
    viewer.screenshot(path=str(OUTPUT/f'preview-{product}-{page.viewport_size["width"]}.png'))
    expect(viewer.locator('#image-preview-title')).to_have_text('主图 1')
    viewer.get_by_role('button',name='下一张 →').click();expect(viewer.locator('#image-preview-title')).to_have_text('主图 2')
    viewer.get_by_role('button',name='放大图片',exact=True).click();expect(viewer).to_have_class(re.compile('is-zoomed'))
    viewer.get_by_role('button',name='恢复适应').click();page.keyboard.press('ArrowLeft');expect(viewer.locator('#image-preview-title')).to_have_text('主图 1')
    page.keyboard.press('Escape');expect(viewer).not_to_be_visible()
    root.locator('[data-image-download="all"]').click();modal=page.locator('[data-image-download-modal]')
    expect(modal.locator('.ss-trigger')).to_contain_text('PNG')
    modal.screenshot(path=str(OUTPUT/f'download-{page.viewport_size["width"]}.png'))
    with page.expect_download() as event:modal.locator('[data-image-download-confirm]').click()
    decoded_download(event.value,'zip');expect(modal).not_to_be_visible()
    root.locator('[data-image-download]').nth(1).click();modal.locator('.ss-trigger').click()
    page.get_by_role('option',name='保持原格式',exact=True).click()
    original=None
    if root_url.endswith(':8000'):
        original=page.request.get(root_url+root.locator('img').first.get_attribute('src')).body()
    with page.expect_download() as event:modal.locator('[data-image-download-confirm]').click()
    if original is not None:assert Path(event.value.path()).read_bytes()==original
    else:decoded_download(event.value,'WEBP')
    page.reload();root.locator('[data-image-download="all"]').click();expect(modal.locator('.ss-trigger')).to_contain_text('保持原格式')
    modal.get_by_role('button',name='取消',exact=True).click()
    if old_batch:
        logs=root.locator('[data-image-logs]').get_attribute('href');src=root.locator('img').first.get_attribute('src')
        root.locator('[data-image-batch-form] .ss-trigger').click()
        page.get_by_role('option',name=re.compile(f'批次 {old_batch}')).click()
        expect(root.locator('[data-image-pending]')).to_contain_text(f'已选择批次 {old_batch}')
        assert root.locator('[data-image-logs]').get_attribute('href')==logs and root.locator('img').first.get_attribute('src')==src
        root.locator('[data-image-batch-form] button[type=submit]').click()
        expect(root.locator('[data-image-loaded]')).to_contain_text(f'当前批次 {old_batch}')
        assert f'/batches/{old_batch}/download' in root.get_attribute('data-image-download-url')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


def check(real=False):
    """运行三个视口的完整界面检查；参数：是否真实本机Web；返回：断言结果。"""
    OUTPUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                fixture=None;errors=[];blocked=[]
                if not real:fixture=ImageToolPageTests();fixture.setUp();c=fixture.case;c.app.config['SETTINGS']=replace(c.settings,task_backend='mysql')
                context=browser.new_context(viewport={'width':width,'height':1000},accept_downloads=True)
                def respond(route):
                    """仅允许本轮工具请求，其他写操作一律拦截；参数：路由；返回：响应。"""
                    request=route.request;url=urlsplit(request.url)
                    if request.method not in ('GET','HEAD','POST') or url.hostname!='127.0.0.1' or (request.method=='POST' and not url.path.startswith('/api/tools/images/')):
                        blocked.append(request.url);route.abort();return
                    if real:route.continue_();return
                    if url.path in {'/api/collection-jobs/active','/api/collections/products/123/jobs'}:
                        route.fulfill(content_type='application/json',body=json.dumps({'jobs':[],'plan':{'modules':[]},'run_status':None}));return
                    headers={key:value for key,value in request.headers.items() if key.lower() in {'content-type','x-collection-token','origin'}}
                    response=c.client.open(url.path+('?' +url.query if url.query else ''),method=request.method,base_url='http://127.0.0.1:18765',headers=headers,data=request.post_data_buffer,buffered=True)
                    try:route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                    finally:response.close()
                try:
                    context.route('**/*',respond);page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    url='http://127.0.0.1:8000' if real else 'http://127.0.0.1:18765'
                    tool_check(page,url);page.goto(url+'/tools/images');page.locator('[data-image-converter]').screenshot(path=str(OUTPUT/f'tool-{width}.png'))
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    gallery_check(page,url,'1050158363561' if real else '123',91 if real else 50,None if real else 40)
                    assert not errors,errors;assert not blocked,blocked
                    print(('real' if real else 'isolated'),width,'passed: upload/ZIP/JPG/original download, independent format memory, gallery/preview, responsive layout')
                finally:
                    context.close()
                    if fixture:fixture.doCleanups()
        finally:browser.close()


if __name__=='__main__':check('--real' in sys.argv)
