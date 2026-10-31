"""新报告及模型重算的隔离浏览器验证；临时数据库，真实AI强制禁用。"""
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
from tests.unit.test_review_analysis_v6 import ReviewV6Tests


def check(output):
    """核对桌面手机、完整报告、引用、导出和动态预览；参数：目录；返回：无。"""
    output.mkdir(parents=True,exist_ok=True);results=[];errors=[];external=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width,height in ((1440,1000),(390,844)):
                case=ReviewV6Tests();case.setUp();task=case.start();case.run_mock(task)
                store=SimpleNamespace(engine=case.engine,tables=case.tables)
                server=make_server('127.0.0.1',0,case.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base=f'http://127.0.0.1:{server.server_port}';page=browser.new_page(viewport={'width':width,'height':height})
                page.on('pageerror',lambda error:errors.append(str(error)))
                def guard(route):
                    """仅允许本机隔离服务；参数：请求；返回：允许或拒绝。"""
                    if route.request.url.startswith(base+'/'):route.continue_()
                    else:external.append(route.request.url);route.abort()
                page.route('**/*',guard)
                try:
                    with patch.object(service,'create_client',side_effect=AssertionError('只读页面不得创建AI客户端')):
                        assert page.goto(base+'/analysis/reviews/tasks/'+task).status==200
                        expect(page.get_by_role('heading',name='用户痛点',exact=True)).to_be_visible()
                        expect(page.locator('.ra-overview-metrics')).to_contain_text('评价样本 2 条')
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'report-{width}.png'))
                        page.locator('.ra-v6-report a[href="#review-8"]').click();expect(page.locator('#review-8')).to_have_attribute('open','')
                        page.locator('#report-citations > summary').click()
                        with page.expect_navigation():page.get_by_role('button',name='重新检查已有报告（不调用AI）').click()
                        page.goto(base+'/analysis/reviews/tasks/'+task+'?version=1');expect(page.get_by_text('历史版本，仅供查看',exact=False)).to_be_visible()
                        export=page.request.get(base+'/analysis/reviews/tasks/'+task+'/export?format=html');assert export.status==200
                        page.set_content(export.text());assert page.locator('form,script,img').count()==0
                        expect(page.get_by_role('heading',name='用户痛点',exact=True)).to_be_visible()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        assert page.request.get(base+'/analysis/reviews/tasks/'+task+'/export?format=md').status==200
                    with patch('ecommerce.infrastructure.storage.collection_browser.collection_store',return_value=store),patch('ecommerce.infrastructure.storage.collection_queries.collection_store',return_value=store),patch.object(service,'create_client') as factory:
                        page.goto(base+'/collections/products/123?module=reviews&reviews_batch=5');page.locator('[data-review-analysis=start]').click()
                        form=page.locator('#review-analysis-dialog [data-ra-start]');expect(form).to_be_visible()
                        expect(form.locator('[data-ra-plan]')).to_contain_text('1 次请求')
                        expect(form.locator('[name=allow_paid]')).not_to_be_checked()
                        first=form.get_attribute('data-plan-hash')
                        form.locator('.ss-trigger').click();page.get_by_role('option',name='deepseek-v4-pro',exact=True).click()
                        expect(form.locator('[data-ra-submit-feedback]')).to_contain_text('计划已更新')
                        assert form.get_attribute('data-plan-hash')!=first
                        output_input=form.locator('[name=output_tokens]');output_input.fill('1024');output_input.dispatch_event('change')
                        expect(form.locator('[data-ra-plan]')).to_contain_text('报告输出上限 1024')
                        expect(form.get_by_role('button',name='开始分析',exact=True)).to_be_enabled();factory.assert_not_called()
                        output_input.fill('393217');output_input.dispatch_event('change')
                        expect(form.locator('[data-ra-submit-feedback]')).to_contain_text('上限')
                        expect(form.get_by_role('button',name='开始分析',exact=True)).to_be_disabled()
                        output_input.fill('8192');output_input.dispatch_event('change')
                        expect(form.locator('[data-ra-plan]')).to_contain_text('报告输出上限 8192')
                        expect(form.get_by_role('button',name='开始分析',exact=True)).to_be_enabled()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'confirmation-{width}.png'))
                        client=Mock();factory.return_value=client
                        with patch.object(service,'launch') as launch:
                            form.locator('[name=allow_paid]').check()
                            with page.expect_navigation():form.get_by_role('button',name='开始分析',exact=True).click()
                            launch.assert_called_once();new=page.url.rsplit('/',1)[-1]
                            created=repository.read(case.settings,new)
                            assert created['task']['model']=='deepseek-v4-pro' and created['task']['parameters']['plan']['planned_calls']==1
                            client.complete_messages.assert_not_called()
                        results.append({'width':width,'report':True,'citations_open_source':True,'history_readonly':True,'export_safe':True,
                            'one_request_plan':True,'model_replan':True,'output_replan':True,'invalid_plan_blocks_submit':True,
                            'selected_model_preserved':True,'unpaid_default':True,'overflow':False})
                finally:page.close();server.shutdown();thread.join(5);case.doCleanups()
        finally:browser.close()
    assert not errors and not external
    (output/'browser.json').write_text(json.dumps({'checks':results,'page_errors':errors,'external_requests':external,'real_ai_calls':0,'formal_writes':0},ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: desktop/mobile direct report, source references, export, model/output replan and selected-model submission; real AI=0')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);check(parser.parse_args().output)
