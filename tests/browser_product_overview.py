"""商品资料页的隔离交互回归，所有请求由临时样例或模拟进度响应。"""
from dataclasses import replace
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from tests.unit.test_collection_browser import CollectionBrowserTests


def check():
    """核对来源守卫、复制、异常、范围参考及手机布局；参数：无；返回：断言。"""
    output=Path('workspace/output/reports/basic_layout/isolated')
    output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                case=CollectionBrowserTests();case.setUp()
                errors=[];blocked=[]
                context=browser.new_context(viewport={'width':width,'height':1000})
                try:
                    case.app.config['SETTINGS']=replace(case.settings,task_backend='mysql')
                    title='很长的商品索引名称，保留完整文字用于展开和复制。'*8
                    table=case.store.tables['products']
                    with case.engine.begin() as connection:
                        connection.execute(table.update().where(table.c.id==1).values(display_title=title))
                    case.batch(70,'basic_info',product_id=1)
                    case.put('product_info_observations',id=70,batch_id=70,title='真实详情标题',
                             captured_at=case.now,price_text='优惠前 ¥3.35 起',sales_text='已售 10万+')
                    case.put('product_current_sections',product_id=1,section='product_info',module='basic_info',batch_id=70)
                    case.put('collection_runs',id=80,product_id=1,status='running',started_at=case.now,task_id=case.task)
                    import os
                    from ecommerce.infrastructure.tasks.ownership import process_started_at
                    from ecommerce.infrastructure.storage.time_values import database_time
                    with case.engine.begin() as connection:
                        task_table=case.store.tables['tasks']
                        connection.execute(task_table.update().where(task_table.c.id==case.task).values(status='running',
                            owner_pid=os.getpid(),owner_process_started_at=database_time(process_started_at(os.getpid()))))
                    job={'id':'b'*32,'product_id':'123','module':'basic_info','state':'succeeded',
                         'done':True,'valid':True,'mode':'single','batch_id':9999,
                         'result_url':'/collections/products/123?batch=9999','result_label':'查看资料',
                         'logs_url':'/logs?task_id='+('b'*32),'message':'成功长说明：D:/example/raw/source.json'}
                    run_status={'state':'running','label':'执行中','task_id':case.task,'started_at':'2026-10-31T20:00:00+08:00',
                                'finished_at':None,'note':'','counts':{'requested':1,'completed':0,'unfinished':1}}
                    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    page.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.__copy=text;}}});")

                    def respond(route):
                        """仅响应本地GET，拒绝任何实际提交；参数：路由；返回：响应。"""
                        request=route.request;url=urlsplit(request.url)
                        if request.method!='GET' or url.hostname!='127.0.0.1':
                            blocked.append(request.method);route.abort();return
                        payload=None
                        if url.path=='/api/collections/products/123/jobs':payload={'jobs':[job],'plan':{'modules':[]},'run_status':run_status}
                        elif url.path=='/api/collection-jobs/active':payload={'jobs':[]}
                        elif url.path=='/api/collection-queue':payload={'items':[],'paused':False}
                        elif url.path=='/api/collection-jobs':payload={'jobs':[],'page':1,'pages':1,'total':0,'page_size':20,'status':'all'}
                        if payload is not None:
                            route.fulfill(content_type='application/json',body=json.dumps(payload));return
                        response=case.client.get(url.path+('?' + url.query if url.query else ''))
                        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)

                    page.route('**/*',respond)
                    page.goto('http://127.0.0.1:8000/collections/products/123')
                    expect(page.locator('.pd-overview')).to_contain_text('2/9')
                    expect(page.locator('.pd-overview')).to_contain_text('执行中')
                    # 既有轮询直接推进页头，不依赖重载页面，也不改变已加载资料来源。
                    run_status.update(state='stopping',label='正在停止')
                    expect(page.locator('[data-product-run-state]')).to_have_text('正在停止',timeout=10000)
                    run_status.update(state='cancelled',label='已停止',finished_at='2026-10-31T20:02:04+08:00',task_id=job['id'])
                    expect(page.locator('[data-product-run-state]')).to_have_text('已停止',timeout=10000)
                    expect(page.locator('[data-product-run-end]')).to_have_text('停止于 2026-10-31 20:02:04')
                    expect(page.locator('[data-product-run-counts]')).to_contain_text('未完成 1 个')
                    assert ('b'*32) in page.locator('[data-product-run-task]').get_attribute('href')
                    expect(page.locator('[data-title-toggle]')).to_be_visible()
                    page.locator('[data-title-toggle]').click()
                    expect(page.locator('[data-title-toggle]')).to_have_attribute('aria-expanded','true')
                    page.locator('[data-copy-target=product-index-title]').click()
                    assert page.evaluate('window.__copy')==title
                    page.locator('[data-title-toggle]').click()
                    logs=page.locator('[data-source-logs]').get_attribute('href')
                    source=page.get_by_role('link',name='查看本批资料',exact=True).get_attribute('href')
                    expect(page.locator('[data-basic-runtime]')).to_be_visible()
                    expect(page.locator('[data-runtime-result]')).to_have_text('查看新资料')
                    assert not page.locator('[data-basic-details]').evaluate('(element)=>element.open')
                    expect(page.locator('[data-success-message]')).not_to_be_visible()
                    page.locator('[data-basic-details]>summary').click()
                    expect(page.locator('[data-success-note]')).to_be_visible()
                    expect(page.locator('[data-success-message]')).to_contain_text('成功长说明')
                    page.locator('[data-basic-details]>summary').click()
                    expect(page.locator('[data-runtime-logs]')).to_be_visible()
                    job.update(logs_url='/logs?task_id='+case.task,batch_id=70)
                    page.reload();expect(page.locator('[data-basic-runtime]')).to_be_hidden()
                    expect(page.locator('[data-runtime-logs]')).to_be_hidden()
                    expect(page.locator('.pd-basic .pd-metrics>div')).to_have_count(2)
                    expect(page.locator('.pd-basic-meta')).to_contain_text('2026-10-25 12:00:00')
                    expect(page.locator('.pd-basic-actions [data-basic-update]')).to_have_css('color','rgb(255, 255, 255)')
                    assert page.locator('.pd-basic-actions').bounding_box()['y']<page.locator('.pd-metrics').bounding_box()['y']
                    if width==1440:
                        assert page.locator('.pd-basic-actions').bounding_box()['x']>page.locator('.pd-basic-identity').bounding_box()['x']
                    assert page.locator('.pd-module-alert').count()==0
                    page.locator('.pd-basic').screenshot(path=str(output/f'basic-{width}.png'))
                    job.update(state='running',done=False,valid=False,task_id='b'*32,message='模拟基本信息更新中',batch_id=None)
                    page.reload();expect(page.locator('[data-basic-update]')).to_have_text('查看当前任务')
                    assert '/tasks/'+('b'*32) in page.locator('[data-basic-update]').get_attribute('href')
                    job.update(state='queued',task_id=None,message='模拟排队')
                    page.reload();expect(page.locator('[data-basic-update]')).to_have_text('查看申请进度')
                    assert 'jobs_status=waiting' in page.locator('[data-basic-update]').get_attribute('href')
                    job.update(state='failed',done=True,valid=False,task_id='b'*32,message='模拟失败，旧来源不变',logs_url='/logs?task_id='+('b'*32),batch_id=9999)
                    page.reload();expect(page.locator('.pd-basic .module-job-status')).to_contain_text('模拟失败')
                    expect(page.locator('[data-basic-update]')).to_have_text('更新基本信息')
                    expect(page.locator('[data-basic-runtime]')).to_be_visible()
                    expect(page.locator('[data-success-note]')).to_be_hidden()
                    expect(page.locator('[data-runtime-logs]')).to_be_visible()
                    assert page.locator('[data-source-logs]').get_attribute('href')==logs
                    assert page.get_by_role('link',name='查看本批资料',exact=True).get_attribute('href')==source
                    assert page.locator('[data-collection-overview]').count()==0
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(output/f'overview-{width}.png'),full_page=True)
                    page.get_by_role('link',name='更新基本信息',exact=True).click()
                    expect(page.locator('#collection-input')).to_have_value('123')
                    assert page.locator('input[name=modules]:checked').evaluate_all('(nodes)=>nodes.map(n=>n.value)')==['basic_info']
                    expect(page.locator('[data-module-source-info]').first).to_be_visible()
                    page.locator('#collection-input').fill('456')
                    expect(page.locator('[data-module-source-info]').first).to_be_hidden()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    assert not errors,errors
                    assert not blocked,blocked
                    print(width,'passed')
                finally:
                    context.close();case.doCleanups()
        finally:browser.close()


if __name__=='__main__':check()
