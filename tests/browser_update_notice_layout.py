"""更新提示布局验证：本机GET加浏览器内存状态，不提交采集或修改业务数据。"""
import copy
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

OUTPUT=Path('workspace/output/reports/update_notice_layout')
PRODUCT='1050158363561'


def check():
    """检查三视口的概况五列和详情提示条件；参数：无；返回：断言结果。"""
    OUTPUT.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                context=browser.new_context(viewport={'width':width,'height':1000})
                page=context.new_page();writes=[];errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                def guard(route):
                    req=route.request
                    if req.method not in ('GET','HEAD'):writes.append(req.url);route.abort()
                    elif urlsplit(req.url).hostname!='127.0.0.1':route.abort()
                    else:route.continue_()
                context.route('**/*',guard)
                try:
                    page.goto(f'http://127.0.0.1:8000/collections/products/{PRODUCT}?module=overview')
                    expect(page.locator('[data-module-update-open]').first).to_be_enabled(timeout=15000)
                    snapshot=page.request.get(f'http://127.0.0.1:8000/api/collections/products/{PRODUCT}/jobs?module_updates=1&overview=1').json()
                    def inject(name,value):
                        page.evaluate('(args)=>document.dispatchEvent(new CustomEvent(args.name,{detail:args.value}))',{'name':name,'value':value})
                    for label,active,complete in [('与有效资料一致',False,True),('等待执行',True,False),('采集中',True,False),('部分完成',False,False),('更新失败',False,False),('已确认无内容',False,True),('未找到入口',False,False)]:
                        model=copy.deepcopy(snapshot['module_updates']);overview=copy.deepcopy(snapshot['overview'])
                        for module in model:
                            item=model[module];item['active']=active
                            item['outcome']={'label':label,'complete':complete,'batch':item['latest_valid'],'message':'本地模拟展示','task_url':None,'logs_url':None}
                        for row in overview['rows']:row.update(attempt_label=label,busy=active,message='本地模拟展示')
                        inject('collection-module-updated',model);inject('collection-overview-updated',overview)
                        expect(page.locator('[data-update-notice]')).to_have_count(0)
                        assert page.locator('[data-overview-row]').count()==9
                        assert page.locator('[data-overview-row]').evaluate_all('(rows)=>rows.every(row=>row.children.length===5)')
                        expect(page.locator('[data-co-attempt]').first).to_have_text(label)
                    inject('collection-module-updated',snapshot['module_updates']);inject('collection-overview-updated',snapshot['overview'])
                    page.locator('[data-overview-row]').first.screenshot(path=str(OUTPUT/f'overview-row-{width}.png'))
                    page.goto(f'http://127.0.0.1:8000/collections/products/{PRODUCT}?module=skus')
                    button=page.locator('[data-module-update-open="skus"]');expect(button).to_be_enabled(timeout=15000)
                    source=button.get_attribute('data-loaded-id');models=page.request.get(f'http://127.0.0.1:8000/api/collections/products/{PRODUCT}/jobs?module_updates=1').json()['module_updates']
                    model=models['skus'];batch=model['latest_valid'];assert batch and str(batch['id'])==source
                    model.update(active=False,outcome={'complete':True,'label':'更新完成','batch':batch,'message':''})
                    inject('collection-module-updated',models);expect(page.locator('[data-update-notice]')).to_have_count(0)
                    model['outcome']['batch']={**batch,'id':int(source)+100000}
                    inject('collection-module-updated',models)
                    notice=page.locator('[data-update-notice="skus"]');expect(notice).to_contain_text('有新批次可查看')
                    assert button.get_attribute('data-loaded-id')==source
                    box=notice.bounding_box();parent=page.locator('[data-sku-page]').bounding_box()
                    assert box['width']>parent['width']*.75,(box,parent)
                    notice.screenshot(path=str(OUTPUT/f'detail-new-{width}.png'))
                    model['outcome'].update(complete=False,label='部分完成',message='本次未完整保存，旧资料保留。')
                    inject('collection-module-updated',models);expect(notice).to_contain_text('部分完成')
                    model.update(active=True);model['outcome'].update(label='采集中',message='')
                    inject('collection-module-updated',models);expect(notice).to_contain_text('采集进度自动刷新')
                    assert not writes,writes;assert not errors,errors
                    print(width,'overview five columns/no duplicate notice; detail same/new/partial/active passed; POST 0')
                finally:context.close()
        finally:browser.close()


if __name__=='__main__':check()
