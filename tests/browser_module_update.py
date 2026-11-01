"""公共更新窗口的隔离浏览器验证；所有提交均为替身，绝不启动真实采集。"""
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
import json
import sys
from playwright.sync_api import sync_playwright, expect
from tests.unit.test_sku_page import SkuPageTests
from ecommerce.infrastructure.storage.collection_browser import browse
from ecommerce.modules.collection.module_update import build
from ecommerce.shared.collection_contract import MODULES
from ecommerce.web.module_update import present
from ecommerce.web.routes.collection_jobs import present as present_job

OUTPUT=Path('workspace/output/reports/module_update')


def new_job():
    """创建仅供隔离页面的排队申请；参数：无；返回：公开状态替身。"""
    return {'id':'b'*32,'product_id':'123','module':'skus','modules':['skus'],'mode':'single','state':'queued','done':False,
        'created_at':'2026-11-01T10:00:00+08:00','message':'模拟更新任务','task_id':None,'task_status':None,
        'options':{'mode':'detail','input':'123','modules':['skus'],'all_reviews':False,'all_questions':False},
        'valid':False,'can_start':False,'failures':[],'counts':{},'progress':{},'started_modules':[],
        'current_module':None,'results':[],'batch_id':None,'total_count':1,'completed_count':0,'skipped_count':0}


def isolated(page,context,width):
    """检查九模块范围、取消、提交和只读状态失败；参数：浏览器与视口；返回：断言。"""
    test=SkuPageTests();test.setUp();c=test.case;test.sku(1)
    c.app.config['SETTINGS']=replace(c.settings,task_backend='mysql')
    state={'job':None,'posts':[],'fail':False,'lost':False};errors=[]
    def packet():
        data=browse(c.settings,'product',product_id='123');jobs=[state['job']] if state['job'] else []
        with c.app.test_request_context():
            return {'jobs':[present_job(j) for j in jobs],'plan':{'modules':[]},'run_status':None,
                'module_updates':{m:present(build(data,jobs,m),'123',m) for m in MODULES}}
    def submit(settings,product,module,**options):
        job=new_job();job.update(module=module,modules=[module]);job['options'].update(modules=[module],
            all_reviews=module=='reviews' and options['unlimited'],all_questions=module=='questions' and options['unlimited'])
        state['job']=job;return job
    def route(route):
        r=route.request;url=urlsplit(r.url)
        assert url.hostname=='127.0.0.1'
        if r.method=='POST':
            assert '/module-update/' in url.path;state['posts'].append((url.path,json.loads(r.post_data)))
        if url.path=='/api/collection-jobs/active':
            with c.app.test_request_context():jobs=[present_job(state['job'])] if state['job'] else []
            route.fulfill(content_type='application/json',body=json.dumps({'jobs':jobs}));return
        if url.path=='/api/collections/products/123/jobs':
            route.fulfill(status=503 if state['fail'] else 200,content_type='application/json',body=json.dumps({'error':'模拟断开'} if state['fail'] else packet()));return
        if url.path.startswith('/api/collection-jobs/'):
            with c.app.test_request_context():job=present_job(state['job'])
            route.fulfill(content_type='application/json',body=json.dumps(job));return
        if '/module-update/' in url.path and r.method=='GET':
            with c.app.test_request_context():job=present_job(state['job']) if state['job'] else None
            route.fulfill(content_type='application/json',body=json.dumps({'job':job}));return
        response=c.client.open(url.path+('?' +url.query if url.query else ''),method=r.method,
            headers={k:v for k,v in r.headers.items() if k.lower() in {'content-type','x-collection-token','x-submission-id','origin'}},
            data=r.post_data_buffer,base_url='http://127.0.0.1:18765',buffered=True)
        try:
            if state['lost'] and r.method=='POST':
                route.fulfill(status=503,content_type='application/json',body='{}');state['lost']=False
            else:route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
        finally:response.close()
    try:
        context.route('**/*',route);page.on('pageerror',lambda error:errors.append(str(error)))
        with patch('ecommerce.modules.collection.web_collection.start_module_update',side_effect=submit):
            for module in MODULES:
                tab='basic' if module=='basic_info' else 'reviews' if module=='review_summary' else module
                state['job']=None
                page.goto(f'http://127.0.0.1:18765/collections/products/123?module={tab}')
                button=page.locator(f'[data-module-update-open="{module}"]');expect(button).to_be_enabled()
                button.click();modal=page.locator('[data-module-update-modal]');expect(modal).to_be_visible()
                expect(modal.locator('[data-update-latest]')).to_be_visible()
                if module in {'reviews','questions'}:
                    page.locator('#module-update-range').select_option('all')
                count=len(state['posts']);modal.get_by_role('button',name='取消',exact=True).click();assert len(state['posts'])==count
                button.click()
                if module in {'reviews','questions'}:page.locator('#module-update-range').select_option('all')
                if module=='video':state['lost']=True
                modal.locator('[data-update-confirm]').evaluate('(button)=>{button.click();button.click()}')
                expect(button).to_have_text('查看更新进度',timeout=10000);assert len(state['posts'])==count+1
                expect(modal.locator('[data-update-task]')).to_be_hidden()
                expect(modal.locator('[data-update-actual-scope]')).to_contain_text('实际范围')
                assert state['posts'][-1][1]['unlimited']==(module in {'reviews','questions'})
                if module in {'reviews','questions'}:expect(modal.locator('[data-update-actual-scope]')).to_contain_text('不限数量')
                modal.screenshot(path=str(OUTPUT/f'{module}-{width}.png'))
                if not page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'):
                    print('overflow',module,width,page.evaluate("[document.documentElement.scrollWidth,innerWidth,scrollX,document.body.scrollWidth,[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1 && e.getBoundingClientRect().width).map(e=>[e.tagName,e.className,e.getBoundingClientRect().right,e.parentElement.className,getComputedStyle(e.parentElement).overflowX]) ]"))
                    print('overflow containers',page.evaluate("[...document.querySelectorAll('body *')].filter(e=>e.clientWidth && e.scrollWidth>e.clientWidth+1).map(e=>[e.tagName,e.className,e.clientWidth,e.scrollWidth,getComputedStyle(e).overflowX])"))
                    page.screenshot(path=str(OUTPUT/f'overflow-{module}-{width}.png'),full_page=True)
                    raise AssertionError((module,width))
            state['job']=None
            page.goto('http://127.0.0.1:18765/collections/products/123?batch=60')
            button=page.locator('[data-module-update-open="skus"]');expect(button).to_be_enabled()
            expect(page.locator('[data-update-history]')).to_contain_text('历史资料')
            button.click();expect(page.locator('[data-update-loaded]')).to_contain_text('批次 60')
            expect(page.locator('[data-update-latest]')).to_contain_text('批次 70')
            state['fail']=True;expect(button).to_be_disabled(timeout=10000)
            expect(page.locator('[data-sku-loaded]')).to_contain_text('当前批次 60')
            assert not errors,errors
            print(width,'nine-module confirm/cancel/double-click/scope/history/read-failure passed; mock POSTs',len(state['posts']))
    finally:
        context.unroute('**/*',route);test.doCleanups()


def check():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width in ((320,) if '--320' in sys.argv else (1440,390,320)):
                context=browser.new_context(viewport={'width':width,'height':1000})
                try:
                    if '--real' not in sys.argv:isolated(context.new_page(),context,width)
                    else:
                        page=context.new_page();blocked=[];errors=[]
                        page.on('pageerror',lambda error:(errors.append(str(error)),print('page error',str(error))))
                        def readonly(route):
                            r=route.request
                            if r.method not in ('GET','HEAD'):
                                blocked.append(r.url);route.abort()
                            elif urlsplit(r.url).hostname!='127.0.0.1':route.abort()
                            else:route.continue_()
                        context.route('**/*',readonly)
                        for tab in ('basic','video','main_images','skus','parameters','detail_images','reviews','questions','overview'):
                            page.goto(f'http://127.0.0.1:8000/collections/products/1050158363561?module={tab}')
                            buttons=page.locator('[data-module-update-open]')
                            for index in range(buttons.count()):
                                button=buttons.nth(index);expect(button).to_be_enabled(timeout=15000);button.click()
                                modal=page.locator('[data-module-update-modal]');expect(modal).to_be_visible()
                                modal.screenshot(path=str(OUTPUT/f'real-{tab}-{index}-{width}.png'))
                                modal.get_by_role('button',name='取消',exact=True).click()
                        assert not blocked,blocked;assert not errors,errors
                        print(width,'actual nine-module and overview dialogs read-only passed; POST 0')
                finally:context.close()
        finally:browser.close()


if __name__=='__main__':check()
