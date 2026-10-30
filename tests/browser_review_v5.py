"""隔离浏览器验证分段回退和人工决定，不访问正式库或真实AI。"""
import argparse
import json
from pathlib import Path
import threading
from unittest.mock import patch
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright,expect
from ecommerce.modules.review_analysis import service,v5
from tests.unit.test_review_analysis_v5 import ReviewV5Tests


def check(output):
    """验证真实页面交互及导出；参数：证据目录；返回：无。"""
    output.mkdir(parents=True,exist_ok=True);checks=[];errors=[];external=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width,height in ((1440,1000),(390,844)):
                case=ReviewV5Tests();case.setUp();task,limit=case.phase_task()
                with patch.object(v5,'MAX_INPUT',limit):case.run_mock(task)
                server=make_server('127.0.0.1',0,case.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base=f'http://127.0.0.1:{server.server_port}';page=browser.new_page(viewport={'width':width,'height':height})
                page.on('pageerror',lambda error:errors.append(str(error)))
                def guard(route):
                    """仅允许当前隔离服务；参数：路由；返回：允许或拒绝。"""
                    if route.request.url.startswith(base+'/'):route.continue_()
                    else:external.append(route.request.url);route.abort()
                page.route('**/*',guard)
                try:
                    with patch.object(service,'create_client',side_effect=AssertionError('页面不允许创建AI客户端')):
                        assert page.goto(base+'/analysis/reviews/tasks/'+task).status==200
                        expect(page.get_by_role('heading',name='已保存的分段报告',exact=True)).to_be_visible()
                        expect(page.locator('#segment-1-finding-f1')).to_be_visible();expect(page.locator('#segment-2-finding-f1')).to_be_visible()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'phase-{width}.png'),full_page=True)
                        form=page.locator('[data-ra-v4-review][data-key="segment:1:opportunity:p1"]')
                        form.locator('xpath=..').locator('summary').click();form.locator('[name=decision]').select_option('adopted')
                        form.locator('[name=reviewer]').fill('隔离测试');form.locator('[name=reason]').fill('核对本段方向条件')
                        form.locator('[name=evidence_checked]').check();form.get_by_role('button',name='保存明确决定（不调用AI）').click()
                        expect(form.locator('.ra-feedback')).to_contain_text('淘汰范围')
                        form.locator('[name=conditions_checked]').check();form.locator('[name=stop_scope]').select_option('current_model')
                        with page.expect_navigation():form.get_by_role('button',name='保存明确决定（不调用AI）').click()
                        expect(page.locator('[data-ra-task]')).to_contain_text('已采用 1 项')
                        # 同名p1仍分属不同段；第二段保持未复核。
                        second=page.locator('[data-ra-v4-review][data-key="segment:2:opportunity:p1"]')
                        assert second.get_attribute('data-revision')=='0'
                        page.goto(base+'/analysis/reviews/products/123/new?batch=5');expect(page.locator('[name=allow_paid]')).not_to_be_checked()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'preview-{width}.png'),full_page=True)
                        exported=page.request.get(base+'/analysis/reviews/tasks/'+task+'/export?format=html');assert exported.status==200
                        page.set_content(exported.text());assert page.locator('form,script').count()==0
                        expect(page.get_by_role('heading',name='已保存的分段报告',exact=True)).to_be_visible()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'export-{width}.png'),full_page=True)
                        checks.append({'width':width,'phase_reports_visible':2,'namespace_isolated':True,'direction_ack_enforced':True,
                            'decision_saved':True,'unpaid_preview':True,'html_export':True,'overflow':False})
                finally:page.close();server.shutdown();thread.join(5);case.doCleanups()
        finally:browser.close()
    assert not errors and not external
    result={'checks':checks,'page_errors':errors,'external_requests':external,'real_ai_calls':0,'formal_database_writes':0}
    (output/'browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: desktop/mobile phase fallback, isolated review identity, acknowledgement and export; AI calls=0')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);check(parser.parse_args().output)
