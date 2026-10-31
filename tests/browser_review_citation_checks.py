"""生成/引用分离、本地检查及原文对照的隔离桌面手机验证；正式数据库和真实AI禁止写入。"""
import argparse
import json
from pathlib import Path
import threading
from unittest.mock import patch
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright,expect
from ecommerce.modules.review_analysis import service
from ecommerce.infrastructure.storage import analysis_store as repository
from tests.unit.test_review_citation_checks import CitationWorkflowTests


def check(output):
    """实际点击本地检查、问题对照、原文定位、历史与导出；参数：证据目录；返回：无。"""
    output.mkdir(parents=True,exist_ok=True);results=[];errors=[];external=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width,height in ((1440,1000),(390,844)):
                case=CitationWorkflowTests();case.setUp();identifier=case.seed(legacy=True);before=repository.read(case.settings,identifier)
                server=make_server('127.0.0.1',0,case.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base=f'http://127.0.0.1:{server.server_port}';page=browser.new_page(viewport={'width':width,'height':height})
                page.on('pageerror',lambda error:errors.append(str(error)))
                def guard(route):
                    """只允许隔离本机页面；参数：请求；返回：放行或拒绝。"""
                    if route.request.url.startswith(base+'/'):route.continue_()
                    else:external.append(route.request.url);route.abort()
                page.route('**/*',guard)
                try:
                    with patch.object(service,'create_client',side_effect=AssertionError('本地检查不得调用AI')),patch.object(service,'launch',side_effect=AssertionError('本地检查不得启动付费进程')):
                        page.goto(base+'/analysis/reviews/tasks/'+identifier)
                        expect(page.get_by_text('此版本保留旧规则检查结果',exact=False)).to_be_visible()
                        assert repository.read(case.settings,identifier)==before
                        assert page.locator('[data-ra-resume]').count()==0
                        with page.expect_navigation():page.get_by_role('button',name='重新检查已有报告（不调用AI）').click()
                        expect(page.locator('#ra-progress')).to_contain_text('报告已生成')
                        expect(page.get_by_text('引用核对：共 66 处，65 处通过，1 处待核对',exact=False)).to_be_visible()
                        expect(page.get_by_text('有效引用涉及 33 条评价',exact=False)).to_be_visible()
                        assert page.locator('[data-ra-resume]').count()==0
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'overview-{width}.png'))
                        problem=page.locator('#citation-check-1');problem.locator('summary').click()
                        expect(problem.get_by_role('heading',name='AI引用')).to_be_visible();expect(problem.get_by_role('heading',name='对应原文')).to_be_visible()
                        expect(problem).to_contain_text('省略号');problem.scroll_into_view_if_needed();page.screenshot(path=str(output/f'comparison-{width}.png'))
                        problem.get_by_role('link',name='定位评价原文').click();expect(page.locator('#review-601')).to_have_attribute('open','')
                        page.goto(base+'/analysis/reviews/tasks/'+identifier+'?version=1');expect(page.get_by_text('历史版本，仅供查看',exact=False)).to_be_visible()
                        assert page.locator('[data-ra-recount]').count()==0
                        expect(page.get_by_text('此版本保留旧规则检查结果',exact=False)).to_be_visible()
                        export=page.request.get(base+'/analysis/reviews/tasks/'+identifier+'/export?format=html');assert export.status==200
                        page.set_content(export.text());assert page.locator('form,script,img').count()==0
                        expect(page.get_by_text('引用核对：共 66 处',exact=False)).to_be_visible()
                        after=repository.read(case.settings,identifier);assert after['calls']==before['calls'] and after['sources']==before['sources'] and after['reports'][0]==before['reports'][0]
                        assert len(after['reports'])==2 and after['task']['status']=='succeeded'
                    results.append({'width':width,'legacy_get_readonly':True,'local_check_updates_state':True,'counts_66_65_1_33':True,'comparison_and_source_link':True,'history_preserved':True,'no_paid_continue':True,'safe_export':True,'original_calls_sources_unchanged':True})
                finally:page.close();server.shutdown();thread.join(5);case.doCleanups()
        finally:browser.close()
    assert not errors and not external
    (output/'browser.json').write_text(json.dumps({'checks':results,'page_errors':errors,'external_requests':external,'real_ai_calls':0,'formal_writes':0},ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: desktop/mobile citation variants, local recheck, comparison, history and exports; real AI=0')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);check(parser.parse_args().output)
