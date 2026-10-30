"""隔离新版报告浏览器验证：临时SQLite和模拟AI，不访问正式业务库。"""
import argparse
import json
from pathlib import Path
import threading
from unittest.mock import patch
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright,expect
from ecommerce.modules.review_analysis import service
from tests.unit.test_review_analysis_v4 import ReviewV4Tests


def check(output):
    """验证桌面手机及真实表单交互；参数：证据目录；返回：保存检查结果。"""
    output.mkdir(parents=True,exist_ok=True);checks=[];errors=[];external=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for width,height in ((1440,1000),(390,844)):
                case=ReviewV4Tests('test_full_flow_read_export_and_separate_review_counts');case.setUp()
                task=case.start();case.run_mock(task)
                server=make_server('127.0.0.1',0,case.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base=f'http://127.0.0.1:{server.server_port}'
                page=browser.new_page(viewport={'width':width,'height':height})
                page.on('pageerror',lambda err:errors.append(str(err)))
                def guard(route):
                    """仅允许当前隔离服务；参数：路由；返回：允许或阻止。"""
                    if route.request.url.startswith(base+'/'):route.continue_()
                    else:external.append(route.request.url);route.abort()
                page.route('**/*',guard)
                try:
                    with patch.object(service,'create_client',side_effect=AssertionError('页面与人工复核禁止创建AI客户端')):
                        response=page.goto(base+'/analysis/reviews/tasks/'+task);assert response.status==200
                        expect(page.get_by_role('heading',name='竞品与选品评价报告',exact=True)).to_be_visible()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'report-{width}.png'))
                        form=page.locator('[data-ra-v4-review][data-key="opportunity:p1"]')
                        form.locator('xpath=..').locator('summary').click()
                        form.locator('[name=decision]').select_option('adopted')
                        form.locator('[name=reviewer]').fill('隔离测试人');form.locator('[name=reason]').fill('测试核对范围和依据')
                        form.get_by_role('button',name='保存明确决定（不调用AI）').click()
                        expect(form.locator('.ra-feedback')).to_contain_text('采用前请明确核对')
                        form.locator('[name=evidence_checked]').check();form.locator('[name=conditions_checked]').check()
                        form.locator('[name=stop_scope]').select_option('current_model')
                        with page.expect_navigation():form.get_by_role('button',name='保存明确决定（不调用AI）').click()
                        expect(page.locator('[data-ra-task]')).to_contain_text('已采用 1 项')
                        source=page.locator('#review-8');source.locator('> summary').click()
                        source.get_by_text('修正观点或排除本条（不调用AI）',exact=True).click()
                        correction=source.locator('[data-ra-v4-correct]')
                        correction.locator('[name=topic]').fill('人工测试主题');correction.locator('[name=reason]').fill('测试本地修正')
                        with page.expect_navigation():correction.get_by_role('button',name='保存修正并本地重统计').click()
                        expect(page.locator('[data-ra-task]')).to_contain_text('尚未生成，可先查看')
                        page.goto(base+'/analysis/reviews/tasks/'+task+'?version=1')
                        expect(page.locator('[data-ra-task]')).to_contain_text('历史版本')
                        assert page.locator('[data-ra-v4-review] button:not([disabled])').count()==0
                        page.screenshot(path=str(output/f'history-{width}.png'))
                        page.goto(base+'/analysis/reviews/products/123/new?batch=5')
                        expect(page.locator('[name=allow_paid]')).not_to_be_checked()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'confirmation-{width}.png'))
                        exported=page.request.get(base+'/analysis/reviews/tasks/'+task+'/export?format=html&version=1')
                        assert exported.status==200
                        page.set_content(exported.text())
                        assert page.locator('script,form').count()==0
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'export-{width}.png'))
                        checks.append({'width':width,'report':True,'manual_ack_enforced':True,'review_saved':True,
                            'local_correction':True,'historical_readonly':True,'unpaid_preview':True,'html_export':True,'overflow':False})
                finally:
                    page.close();server.shutdown();thread.join(5);case.doCleanups()
        finally:browser.close()
    assert not errors and not external
    result={'checks':checks,'page_errors':errors,'external_requests':external,'real_ai_calls':0,'formal_database_writes':0,'synthetic_only':True}
    (output/'browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('desktop/mobile report, explicit review, local correction, history, confirmation and export: PASS; paid calls=0')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    check(parser.parse_args().output)
