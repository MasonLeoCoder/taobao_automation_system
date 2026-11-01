"""SKU更新确认及状态流转的隔离浏览器验证，真实模式绝不提交更新。"""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright,expect
from ecommerce.infrastructure.storage.collection_browser import browse
from ecommerce.modules.collection.sku_update import build
from ecommerce.web.sku_update import present
from ecommerce.web.routes.collection_jobs import present as present_job
from tests.unit.test_sku_page import SkuPageTests

OUTPUT=Path('workspace/output/reports/sku_update')


def new_job(state='queued',identifier='b'*32,done=False):
    """准备明确单SKU申请；参数：状态、编号及终态；返回：脱敏公开描述。"""
    return {'id':identifier,'product_id':'123','module':'skus','modules':['skus'],'mode':'single','state':state,'done':done,
            'created_at':'2026-11-01T10:00:00+08:00' if identifier=='b'*32 else '2026-11-01T12:00:00+08:00',
            'message':'模拟SKU更新任务','task_id':None if state=='queued' else identifier,'task_status':None,
            'options':{'mode':'detail','input':'123','modules':['skus'],'all_reviews':False,'all_questions':False},
            'valid':False,'can_start':False,'failures':[],'counts':{},'progress':{},'started_modules':['skus'],
            'current_module':'skus','results':[],'batch_id':None,'total_count':1,'completed_count':0,'skipped_count':0}


def isolated(page,context,width):
    """检查不提交取消、重复点击、刷新恢复及新旧结果；参数：页面/上下文/视口；返回：无。"""
    test=SkuPageTests();test.setUp();c=test.case
    c.app.config['SETTINGS']=replace(c.settings,task_backend='mysql')
    test.sku(1);test.sku(2)
    state={'job':None,'fail':False,'posts':[]};errors=[]
    def packet():
        """提供真实批次读取和模拟执行状态；参数：无；返回：只读轮询响应。"""
        data=browse(c.settings,'product',product_id='123');jobs=[state['job']] if state['job'] else []
        with c.app.test_request_context():
            return {'jobs':[present_job(job) for job in jobs],'plan':{'modules':[]},'run_status':None,
                    'sku_update':present(build(data,jobs),'123')}
    def submit(settings,product,**options):
        """只模拟申请，不启动采集；参数：接口原参数；返回：固定申请。"""
        assert product=='123';state['job']=new_job();return state['job']
    patcher=patch('ecommerce.modules.collection.web_collection.start_sku_update',side_effect=submit);patcher.start()
    def route(route):
        """所有请求仅交给隔离服务或模拟状态；参数：路由；返回：响应。"""
        r=route.request;url=urlsplit(r.url)
        if url.hostname!='127.0.0.1':raise AssertionError('unexpected remote request')
        if r.method=='POST':
            assert url.path=='/api/collections/products/123/sku-update';assert json.loads(r.post_data)=={'confirm':True}
            state['posts'].append(r.post_data)
        elif r.method not in ('GET','HEAD'):raise AssertionError('unexpected write')
        if url.path=='/api/collection-jobs/active':
            with c.app.test_request_context():jobs=[present_job(state['job'])] if state['job'] and not state['job']['done'] else []
            route.fulfill(content_type='application/json',body=json.dumps({'jobs':jobs}));return
        if url.path=='/api/collections/products/123/jobs':
            route.fulfill(status=503 if state['fail'] else 200,content_type='application/json',body=json.dumps({'error':'模拟断开'} if state['fail'] else packet()));return
        if url.path.startswith('/api/collection-jobs/') or '/jobs/' in url.path:
            with c.app.test_request_context():job=present_job(state['job'])
            route.fulfill(content_type='application/json',body=json.dumps(job));return
        response=c.client.open(url.path+('?' +url.query if url.query else ''),method=r.method,
            headers={key:value for key,value in r.headers.items() if key.lower() in {'content-type','x-collection-token','x-submission-id','origin'}},
            data=r.post_data_buffer,base_url='http://127.0.0.1:18765',buffered=True)
        try:route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
        finally:response.close()
    try:
        context.route('**/*',route);page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto('http://127.0.0.1:18765/collections/products/123?batch=60')
        root=page.locator('[data-sku-page]');button=root.locator('[data-sku-update-open]');modal=page.locator('[data-sku-update-modal]')
        expect(button).to_be_enabled();button.click();expect(modal.locator('[data-sku-confirm-loaded]')).to_contain_text('批次 60')
        expect(modal.locator('[data-sku-confirm-latest]')).to_contain_text('批次 70')
        modal.screenshot(path=str(OUTPUT/f'confirm-{width}.png'))
        modal.get_by_role('button',name='取消',exact=True).click();assert not state['posts']
        logs=root.locator('[data-sku-logs]').get_attribute('href')
        button.click();modal.locator('[data-sku-update-confirm]').evaluate('(button)=>{button.click();button.click()}')
        expect(button).to_have_text('查看更新进度',timeout=10000);assert len(state['posts'])==1
        expect(modal.locator('[data-sku-modal-state]')).to_have_text('等待执行',timeout=10000)
        expect(modal.locator('[data-sku-modal-task]')).to_be_hidden()
        modal.locator('[data-sku-update-close]').last.click();page.reload();expect(button).to_have_text('查看更新进度')
        button.click();assert len(state['posts'])==1
        state['job']=new_job('running');expect(modal.locator('[data-sku-modal-state]')).to_have_text('采集中',timeout=10000)
        expect(modal.locator('[data-sku-modal-task]')).to_have_attribute('href','/tasks/'+('b'*32))
        c.batch(80,'skus',product_id=1,status='partial',task_id='b'*32,captured_at=datetime(2026,11,1,11))
        c.put('sku_observations',id=800,batch_id=80,position=1,sku_name='部分新结果',price_status='not_read')
        state['job']=new_job('needs_attention',done=True)
        expect(modal.locator('[data-sku-modal-state]')).to_have_text('部分完成',timeout=10000)
        expect(modal.locator('[data-sku-confirm-latest]')).to_contain_text('批次 70')
        expect(root.locator('[data-sku-loaded]')).to_contain_text('当前批次 60')
        assert root.locator('[data-sku-logs]').get_attribute('href')==logs
        modal.screenshot(path=str(OUTPUT/f'partial-{width}.png'))
        state['fail']=True;expect(button).to_be_disabled(timeout=10000)
        expect(root.locator('[data-sku-loaded]')).to_contain_text('当前批次 60')
        state['fail']=False;expect(button).to_be_enabled(timeout=10000)
        state['job']=new_job('failed','c'*32,True)
        expect(modal.locator('[data-sku-modal-state]')).to_have_text('更新失败',timeout=10000)
        c.batch(90,'skus',product_id=1,status='complete',task_id='c'*32,captured_at=datetime(2026,11,1,13))
        c.put('sku_observations',id=900,batch_id=90,position=1,sku_name='完整新规格',stock_status='selectable',price_status='collected',price_value=6,currency='CNY')
        with c.engine.begin() as conn:
            table=c.store.tables['product_current_sections'];conn.execute(table.update().where(table.c.module=='skus').values(batch_id=90))
        state['job']=new_job('succeeded','c'*32,True)
        expect(modal.locator('[data-sku-modal-state]')).to_have_text('更新完成',timeout=10000)
        expect(root.locator('[data-sku-loaded]')).to_contain_text('当前批次 60')
        expect(modal.locator('[data-sku-update-confirm]')).to_be_hidden()
        modal.locator('[data-sku-modal-result]').click();expect(root.locator('[data-sku-loaded]')).to_contain_text('当前批次 90')
        expect(root).to_contain_text('完整新规格');assert len(state['posts'])==1
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');assert not errors,errors
        print(width,'isolated passed: confirm/cancel, one POST, queued refresh, partial/failure, disconnected, explicit new batch')
    finally:patcher.stop();context.unroute('**/*',route);test.doCleanups()


def check(real=False):
    """运行三视口；参数：真实模式只打开并取消确认；返回：断言结果。"""
    OUTPUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                context=browser.new_context(viewport={'width':width,'height':1000});page=context.new_page()
                try:
                    if not real:isolated(page,context,width);continue
                    blocked=[]
                    def guard(route):
                        """真实验证只允许本机读取；参数：路由；返回：放行或拦截。"""
                        r=route.request
                        if r.method not in ('GET','HEAD') or urlsplit(r.url).hostname!='127.0.0.1':blocked.append(r.url);route.abort()
                        else:route.continue_()
                    context.route('**/*',guard)
                    page.goto('http://127.0.0.1:8000/collections/products/1050158363561?batch=92')
                    root=page.locator('[data-sku-page]');button=root.locator('[data-sku-update-open]');expect(button).to_be_enabled()
                    root.locator('.pb-heading').screenshot(path=str(OUTPUT/f'real-heading-{width}.png'))
                    button.click();modal=page.locator('[data-sku-update-modal]')
                    expect(modal.locator('[data-sku-confirm-latest]')).to_contain_text('批次 92')
                    modal.screenshot(path=str(OUTPUT/f'real-confirm-{width}.png'))
                    modal.get_by_role('button',name='取消',exact=True).click();expect(modal).not_to_be_visible()
                    assert not blocked,blocked;assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    print(width,'real readonly confirmation passed; no collection request')
                finally:context.close()
        finally:browser.close()


if __name__=='__main__':check('--real' in sys.argv)
