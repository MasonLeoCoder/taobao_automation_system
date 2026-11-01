"""采集概况桌面/手机与单模块提交隔离回归，无真实采集或AI。"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright,expect
from tests.unit import test_collection_browser as fixtures


def check():
    """核对九行/筛选/排队/错误恢复和单模块请求；参数：无；返回：断言。"""
    output=Path('workspace/output/reports/collection_overview/isolated');output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                case=fixtures.CollectionBrowserTests();case.setUp();states=[];posts=[];errors=[];failed=[False]
                context=browser.new_context(viewport={'width':width,'height':1050})
                try:
                    case.app.config['SETTINGS']=replace(case.settings,task_backend='mysql')
                    case.batch(40,'video',product_id=1,status='no_video')
                    case.put('product_current_sections',product_id=1,section='video',module='video',batch_id=40)
                    case.put('tasks',id='b'*32,name='collection.taobao.detail.open',status='cancelled',started_at=case.now,finished_at=case.now)
                    case.put('collection_runs',id=80,product_id=1,task_id='b'*32,status='running',started_at=case.now)
                    case.put('collection_steps',id=80,run_id=80,module='questions',requested=True,status='running')
                    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))

                    def respond(route):
                        """提供本地脱敏页面，唯一POST仅记录请求并模拟排队；参数：路由；返回：响应。"""
                        request=route.request;url=urlsplit(request.url)
                        assert url.hostname=='127.0.0.1'
                        if request.method=='POST':
                            assert url.path=='/api/collections/products/123/jobs'
                            body=request.post_data_json;posts.append(body);assert body=={'module':'video'}
                            state={'id':'c'*32,'product_id':'123','module':'video','mode':'single','modules':['video'],
                                   'state':'queued','done':False,'valid':False,'batch_id':None,'task_id':None,
                                   'created_at':'2026-11-01T01:00:00+08:00','message':'模拟申请排队','status_url':'/api/collection-jobs/'+('c'*32),'logs_url':'/logs?task_id='+('c'*32)}
                            states[:]=[state];route.fulfill(status=202,content_type='application/json',body=json.dumps(state));return
                        assert request.method=='GET'
                        if url.path=='/api/collections/products/123/jobs' and failed[0]:
                            route.fulfill(status=503,content_type='application/json',body=json.dumps({'error':'模拟读取失败'}));return
                        if url.path=='/api/collection-jobs/active':
                            route.fulfill(content_type='application/json',body='{"jobs":[]}');return
                        if url.path=='/api/collection-jobs/'+('c'*32):
                            route.fulfill(content_type='application/json',body=json.dumps(states[0]));return
                        response=case.client.get(url.path+('?' + url.query if url.query else ''))
                        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)

                    page.route('**/*',respond)
                    with patch('ecommerce.modules.collection.web_collection.recent',side_effect=lambda *a:deepcopy(states)):
                        page.goto('http://127.0.0.1:8000/collections/products/123?module=overview')
                        expect(page.locator('[data-overview-row]')).to_have_count(9)
                        expect(page.locator('[data-overview-row]:visible')).to_have_count(9)
                        expect(page.locator('[data-co-count=ready]')).to_have_text('2')
                        expect(page.locator('[data-co-count=issues]')).to_have_text('2')
                        video=page.locator('[data-overview-row=video]');questions=page.locator('[data-overview-row=questions]')
                        expect(questions.locator('[data-co-attempt]')).to_have_text('已停止')
                        expect(video.locator('[data-overview-collect]')).to_be_enabled()
                        expect(video.locator('[data-overview-collect]')).to_have_css('color','rgb(255, 255, 255)')
                        source=video.locator('[data-co-source]').get_attribute('href')
                        page.locator('[data-co-filter=issues]').click();expect(page.locator('[data-overview-row]:visible')).to_have_count(2)
                        page.locator('[data-co-filter=all]').click();expect(page.locator('[data-overview-row]:visible')).to_have_count(9)
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.locator('[data-collection-overview]').screenshot(path=str(output/f'overview-{width}.png'))
                        video.locator('[data-overview-collect]').click()
                        expect(video.locator('[data-co-active]')).to_be_visible(timeout=10000)
                        expect(video.locator('[data-co-active]')).to_have_text('查看申请进度')
                        expect(video.locator('[data-overview-collect]')).to_be_hidden()
                        assert '/modules/collection' in video.locator('[data-co-active]').get_attribute('href')
                        assert len(posts)==1 and posts[0]=={'module':'video'}
                        assert video.locator('[data-co-source]').get_attribute('href')==source
                        failed[0]=True
                        expect(page.locator('[data-co-feedback]')).to_be_visible(timeout=10000)
                        expect(page.locator('[data-overview-row=skus] [data-overview-collect]')).to_be_disabled()
                        expect(page.locator('[data-co-count=ready]')).to_have_text('2')
                        failed[0]=False
                        expect(page.locator('[data-co-feedback]')).to_be_hidden(timeout=10000)
                        expect(page.locator('[data-overview-row=skus] [data-overview-collect]')).to_be_enabled()
                        states[0].update(state='failed',done=True,message='模拟执行失败，旧资料保留')
                        expect(video.locator('[data-co-attempt]')).to_have_text('采集失败',timeout=10000)
                        expect(video.locator('[data-overview-collect]')).to_be_visible()
                        expect(video.locator('[data-overview-collect]')).to_be_enabled()
                        expect(video.locator('[data-co-active]')).to_be_hidden()
                        assert video.locator('[data-co-source]').get_attribute('href')==source
                        assert len(posts)==1
                        page.get_by_role('link',name='基本信息',exact=True).click()
                        expect(page.locator('[data-collection-overview]')).to_have_count(0)
                        expect(page.locator('.pd-basic')).to_be_visible()
                        assert not errors,errors
                    print(width,'passed: 9 modules, filters, single POST, queued guard, errors and recovery')
                finally:context.close();case.doCleanups()
        finally:browser.close()


if __name__=='__main__':check()
