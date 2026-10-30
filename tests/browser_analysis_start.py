"""分析提交交互验证：实际隔离API、模拟派发，禁止真实AI与正式库写入。"""
import argparse
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock,patch
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright,expect
from ecommerce.modules.review_analysis import service
from ecommerce.infrastructure.storage import analysis_store as repository
from tests.unit.test_review_analysis_v5 import ReviewV5Tests


def check(output):
    """核对桌面手机的费用、选择和重复点击；参数：证据路径；返回：无。"""
    output.mkdir(parents=True,exist_ok=True);checks=[];errors=[];external=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width,height in ((1440,1000),(390,844)):
                case=ReviewV5Tests();case.setUp();old=case.start()
                repository.update_task(case.settings,old,status='failed',active_key=None,owner=None,message='隔离接口故障')
                original=repository.read(case.settings,old);store=SimpleNamespace(engine=case.engine,tables=case.tables)
                server=make_server('127.0.0.1',0,case.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base=f'http://127.0.0.1:{server.server_port}';page=browser.new_page(viewport={'width':width,'height':height});requests=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def guard(route):
                    """只允许隔离本机服务；参数：路由；返回：允许或拒绝。"""
                    if route.request.url.startswith(base+'/'):route.continue_()
                    else:external.append(route.request.url);route.abort()
                page.route('**/*',guard)
                page.on('request',lambda request:requests.append(request.post_data_json) if request.method=='POST' and request.url==base+'/api/analysis/reviews/products/123' else None)
                def open_form():
                    """打开真实动态确认弹窗；参数：无；返回：表单。"""
                    page.goto(base+'/collections/products/123?module=reviews&reviews_batch=5')
                    page.locator('[data-review-analysis=start]').click()
                    form=page.locator('#review-analysis-dialog [data-ra-start]');expect(form).to_be_visible();return form
                client=Mock();client.complete.side_effect=AssertionError('浏览器测试不得调用AI')
                try:
                    with patch('ecommerce.infrastructure.storage.collection_browser.collection_store',return_value=store),patch('ecommerce.infrastructure.storage.collection_queries.collection_store',return_value=store),patch.object(service,'launch') as launch,patch.object(service,'create_client',return_value=client):
                        form=open_form();button=form.get_by_role('button',name='开始分析',exact=True);feedback=form.locator('[data-ra-submit-feedback]')
                        button.click();expect(feedback).to_contain_text('费用确认');expect(feedback).to_be_visible();assert len(requests)==0
                        form.locator('[name=allow_paid]').check();button.click()
                        expect(form.locator('[data-ra-existing]')).to_be_visible();expect(form.locator('[data-ra-existing-title]')).to_contain_text('失败')
                        assert len(requests)==1;launch.assert_not_called()
                        expect(form.locator('[data-ra-continue]')).to_be_visible()
                        form.get_by_role('button',name='从头重新分析（再次计费）').click()
                        expect(feedback).to_contain_text('先勾选');assert len(requests)==1
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'choice-{width}.png'))
                        form.locator('[name=restart_paid]').check()
                        with page.expect_navigation():
                            form.locator('[data-ra-restart]').evaluate('(button)=>{button.click();button.click()}')
                        new=page.url.rsplit('/',1)[-1];assert new!=old
                        assert len(requests)==2 and requests[-1]['force'] is True;launch.assert_called_once()
                        assert repository.read(case.settings,old)==original
                        form=open_form();form.locator('[name=allow_paid]').check()
                        with page.expect_navigation():form.get_by_role('button',name='开始分析',exact=True).click()
                        assert page.url.endswith('/'+new);assert len(requests)==3;launch.assert_called_once()
                        repository.update_task(case.settings,new,status='succeeded',active_key=None,owner=None)
                        form=open_form();form.locator('[name=allow_paid]').check();form.get_by_role('button',name='开始分析',exact=True).click()
                        expect(form.locator('[data-ra-existing-title]')).to_contain_text('已完成');expect(form.locator('[data-ra-continue]')).to_be_hidden()
                        expect(form.locator('[data-ra-view]')).to_have_text('查看已有报告')
                        repository.update_task(case.settings,new,status='partial',active_key=None,owner=None)
                        form=open_form();form.locator('[name=allow_paid]').check();form.get_by_role('button',name='开始分析',exact=True).click()
                        expect(form.locator('[data-ra-existing-title]')).to_contain_text('部分完成')
                        with page.expect_navigation():form.locator('[data-ra-continue]').click()
                        assert page.url.endswith('#analysis-resume');expect(page.locator('[data-ra-resume]')).to_be_visible();launch.assert_called_once()
                        form=open_form();form.locator('[name=allow_paid]').check()
                        def reject(route):
                            """模拟安全API错误，不写库；参数：路由；返回：拒绝。"""
                            route.fulfill(status=400,json={'error':'脱敏参数不受支持，请刷新后核对。'})
                        page.route('**/api/analysis/reviews/products/123',reject)
                        advanced=form.locator('details').filter(has=page.locator('summary',has_text='高级设置')).first
                        assert advanced.get_attribute('open') is None
                        form.get_by_role('button',name='开始分析',exact=True).click()
                        expect(form.locator('[data-ra-submit-feedback]')).to_contain_text('脱敏参数不受支持')
                        expect(form.locator('[data-ra-submit-feedback]')).to_be_visible();expect(form.get_by_role('button',name='开始分析',exact=True)).to_be_enabled()
                        expect(form.locator('[name=force]')).not_to_be_checked()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'error-{width}.png'))
                        page.unroute('**/api/analysis/reviews/products/123',reject)
                        form.get_by_role('button',name='开始分析',exact=True).click();expect(form.locator('[data-ra-existing]')).to_be_visible()
                        form.locator('[name=restart_paid]').check()
                        def lost_response(route):
                            """模拟代理返回非JSON，确认不自动重试或沿用强制意图；参数：路由；返回：异常响应。"""
                            assert route.request.post_data_json['force'] is True
                            route.fulfill(status=502,body='upstream unavailable',content_type='text/plain')
                        page.route('**/api/analysis/reviews/products/123',lost_response)
                        form.locator('[data-ra-restart]').click();expect(form.locator('[data-ra-submit-feedback]')).to_contain_text('未返回可识别')
                        expect(form.locator('[name=force]')).not_to_be_checked()
                        page.unroute('**/api/analysis/reviews/products/123',lost_response)
                        form.get_by_role('button',name='开始分析',exact=True).click();expect(form.locator('[data-ra-existing]')).to_be_visible()
                        assert requests[-1]['force'] is False;launch.assert_called_once()
                        client.complete.assert_not_called()
                        checks.append({'width':width,'old_task_id':old,'new_synthetic_task_id':new,'unpaid_blocked':True,'failed_choice':True,
                            'restart_fee_confirmed':True,'double_click_one_launch':True,'active_reused':True,'success_choice':True,
                            'partial_continue_preview':True,'error_visible_outside_fold':True,'lost_response_clears_restart_intent':True,'old_unchanged':True,'overflow':False})
                finally:page.close();server.shutdown();thread.join(5);case.doCleanups()
        finally:browser.close()
    assert not errors and not external
    (output/'browser.json').write_text(json.dumps({'checks':checks,'page_errors':errors,'external_requests':external,'real_ai_calls':0,'formal_database_writes':0},ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: desktop/mobile start choices, restart fee, active reuse, duplicate prevention, continue preview and visible errors; real AI=0')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);check(parser.parse_args().output)
