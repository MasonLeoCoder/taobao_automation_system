"""混排原文依据桌面/手机及离线HTML验收，只用临时库与模拟AI。"""
import argparse
import json
from pathlib import Path
import threading
from unittest.mock import patch
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright,expect
from ecommerce.modules.review_analysis import service
from ecommerce.infrastructure.storage import analysis_store as repository
from tests.fixtures.review_citations_v6 import mixed_report_sample
from tests.unit.test_review_analysis_v6 import ReviewV6Tests


def check(output):
    """验证默认关闭、分析可见、鼠标键盘及导出；参数：证据目录；返回：无。"""
    output.mkdir(parents=True,exist_ok=True);results=[];errors=[];external=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width,height in ((1440,1000),(390,844)):
                case=ReviewV6Tests();case.setUp();identifier=case.start();markdown=mixed_report_sample()
                with patch('tests.unit.test_review_analysis_v6.report_for',return_value=markdown):case.run_mock(identifier)
                before=repository.read(case.settings,identifier)
                server=make_server('127.0.0.1',0,case.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base=f'http://127.0.0.1:{server.server_port}';page=browser.new_page(viewport={'width':width,'height':height})
                page.on('pageerror',lambda error:errors.append(str(error)))
                def guard(route):
                    """拒绝隔离服务以外的请求；参数：请求；返回：无。"""
                    if route.request.url.startswith(base+'/'):route.continue_()
                    else:external.append(route.request.url);route.abort()
                page.route('**/*',guard)
                try:
                    with patch.object(service,'create_client',side_effect=AssertionError('阅读不得调用AI')),patch.object(service,'launch',side_effect=AssertionError('阅读不得派发')):
                        assert page.goto(base+'/analysis/reviews/tasks/'+identifier).status==200
                        document=page.locator('[data-report-document]');evidence=document.locator('.ui-report-evidence')
                        expect(evidence).to_have_count(7);expect(document.locator('a[href^="#review-"]')).to_have_count(11)
                        assert evidence.evaluate_all('(nodes)=>nodes.every(n=>!n.open)')
                        expect(document.get_by_text('场景甲：',exact=True)).to_be_visible()
                        expect(document.get_by_text('场景乙：',exact=True)).to_be_visible()
                        expect(document.get_by_text('后续分析始终可见。',exact=True)).to_be_visible()
                        expect(document.get_by_text('子场景：',exact=True)).to_be_visible()
                        first=evidence.first;expect(first.locator('summary')).to_have_text('查看原文依据（2处引用）')
                        expect(first.locator('a').first).not_to_be_visible()
                        first.locator('summary').focus();page.keyboard.press('Enter');expect(first.locator('a').first).to_be_visible()
                        first.locator('summary').click();expect(first.locator('a').first).not_to_be_visible()
                        document.get_by_text('场景甲：',exact=True).scroll_into_view_if_needed()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'mixed-closed-{width}.png'))
                        first.locator('summary').click();page.screenshot(path=str(output/f'mixed-open-{width}.png'))
                        first.locator('a[href="#review-8"]').click();expect(page.locator('#review-8')).to_have_attribute('open','')
                        # 历史选择与三种导出只读取原稿和原检查，不追加任何业务记录。
                        assert page.goto(base+'/analysis/reviews/tasks/'+identifier+'?version=1').status==200
                        md=page.request.get(base+'/analysis/reviews/tasks/'+identifier+'/export?format=md')
                        assert md.status==200 and md.text()==markdown
                        exported=page.request.get(base+'/analysis/reviews/tasks/'+identifier+'/export?format=html')
                        assert exported.status==200;page.set_content(exported.text())
                        document=page.locator('[data-report-document]');offline=document.locator('.ui-report-evidence')
                        expect(offline).to_have_count(7);assert page.locator('script,form,img').count()==0
                        expect(document.get_by_text('场景甲：',exact=True)).to_be_visible()
                        expect(offline.first.locator('a').first).not_to_be_visible()
                        offline.first.locator('summary').click();expect(offline.first.locator('a').first).to_be_visible()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        exported_json=page.request.get(base+'/analysis/reviews/tasks/'+identifier+'/export');assert exported_json.status==200
                        assert repository.read(case.settings,identifier)==before
                    results.append({'width':width,'closed_groups':7,'source_links':11,'analysis_visible':True,'mouse_keyboard':True,
                        'source_jump':True,'offline_html':True,'markdown_unchanged':True,'database_unchanged':True,'overflow':False})
                finally:page.close();server.shutdown();thread.join(5);case.doCleanups()
        finally:browser.close()
    assert not errors and not external
    (output/'browser.json').write_text(json.dumps({'checks':results,'page_errors':errors,'external_requests':external,'real_ai_calls':0,'formal_writes':0},ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: desktop/mobile mixed evidence, visible analysis, source jump, history and offline exports; AI=0')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);check(parser.parse_args().output)
