"""视频页批次切换、响应式布局与本地下载的隔离回归。"""
from dataclasses import replace
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright,expect
from tests.unit import test_video_page as fixtures


def check():
    """只用隔离文件核对展示和附件响应；参数：无；返回：断言。"""
    output=Path('workspace/output/reports/video_layout/isolated');output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                test=fixtures.VideoPageTests();test.setUp();c=test.case;errors=[];blocked=[];documents=[]
                c.app.config['SETTINGS']=replace(c.settings,task_backend='mysql')
                context=browser.new_context(viewport={'width':width,'height':1000},accept_downloads=True)
                def respond(route):
                    """提供本地样例与只读进度；参数：路由；返回：响应。"""
                    r=route.request;url=urlsplit(r.url)
                    if r.method not in ('GET','HEAD') or url.hostname!='127.0.0.1':blocked.append(r.method);route.abort();return
                    if r.is_navigation_request():documents.append(url.path+'?'+url.query)
                    payload=None
                    if url.path=='/api/collections/products/123/jobs':payload={'jobs':[],'plan':{'modules':[]},'run_status':None}
                    elif url.path=='/api/collection-jobs/active':payload={'jobs':[]}
                    if payload is not None:route.fulfill(content_type='application/json',body=json.dumps(payload));return
                    headers={'Range':r.headers['range']} if r.headers.get('range') else {}
                    response=c.client.open(url.path+('?' +url.query if url.query else ''),method=r.method,headers=headers,buffered=True)
                    try:route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                    finally:response.close()
                try:
                    context.route('**/*',respond)
                    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto('http://127.0.0.1:18765/collections/products/123?module=video')
                    root=page.locator('[data-video-page]');expect(root.locator('[data-video-loaded]')).to_contain_text('当前批次 50')
                    expect(root.locator('.ss-trigger')).to_contain_text('批次 50');expect(root.locator('[data-video-sort]')).to_have_count(0)
                    expect(root.locator('video')).to_have_attribute('preload','metadata')
                    assert root.locator('video').get_attribute('autoplay') is None
                    expect(root.locator('[data-video-download]')).to_have_css('color','rgb(255, 255, 255)')
                    if width==1440:assert root.locator('video').bounding_box()['width']>500
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    root.screenshot(path=str(output/f'video-{width}.png'))
                    src=root.locator('video').get_attribute('src');logs=root.locator('[data-video-logs]').get_attribute('href');initial=len(documents)
                    root.locator('.ss-trigger').click();page.get_by_role('combobox',name='查找视频采集批次',exact=True).fill('40')
                    page.get_by_role('option',name=re.compile('批次 40')).click()
                    expect(root.locator('[data-video-pending]')).to_contain_text('已选择批次 40')
                    assert len(documents)==initial and root.locator('video').get_attribute('src')==src
                    assert root.locator('[data-video-logs]').get_attribute('href')==logs
                    root.locator('[data-video-batch-form] button[type=submit]').click()
                    expect(root.locator('video')).to_have_attribute('src','/collections/files/40')
                    assert ('b'*32) in root.locator('[data-video-logs]').get_attribute('href')
                    # 浏览器附件请求可能越过模拟路由；隔离检查只核对绑定和服务响应，真实下载另走本地Web验收。
                    link=urlsplit(root.locator('[data-video-download]').get_attribute('href'))
                    assert link.path=='/collections/files/40' and link.query=='download=1'
                    with c.client.get(link.path+'?'+link.query,buffered=True) as response:
                        assert response.data==b'isolated-video-40'
                        assert 'sample-40.mp4' in response.headers['Content-Disposition']
                    root.locator('[data-video-source]>summary').click();assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    (c.settings.workspace/'output/video/sample-40.mp4').unlink()
                    page.reload();expect(root.locator('[data-video-local]')).to_have_text('本地文件缺失')
                    expect(root.locator('[data-video-download]')).to_have_count(0);expect(root.locator('video')).to_have_count(0)
                    assert not errors,errors;assert not blocked,blocked
                    print(width,'passed: layout, pending source, explicit switch, downloaded bytes/name, missing file; no collection')
                finally:context.close();test.doCleanups()
        finally:browser.close()


if __name__=='__main__':check()
