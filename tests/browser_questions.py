"""问答弹窗桌面/手机隔离验收，模拟模型，不发起真实AI或采集。"""
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright,expect
from tests.unit.test_question_analysis import QuestionTests,sample_report
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.modules.question_analysis import runtime,service
from unittest.mock import Mock


def check():
    """检查入口、模型重算、费用守卫、原文折叠及下载；参数：无；返回：断言。"""
    output=Path('workspace/output/reports/question_analysis/browser');output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                case=QuestionTests();case.setUp();server=make_server('127.0.0.1',0,case.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                page=browser.new_page(viewport={'width':width,'height':950});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                try:
                    store=SimpleNamespace(engine=case.engine,tables=case.tables)
                    with patch.object(repository,'collection_store',return_value=store),patch('ecommerce.infrastructure.storage.collection_browser.collection_store',return_value=store),patch('ecommerce.infrastructure.storage.collection_queries.collection_store',return_value=store):
                        base=f'http://127.0.0.1:{server.server_port}'
                        page.goto(base+'/collections/products/123?module=questions&batch=6');page.locator('[data-qa-open]').click()
                        dialog=page.locator('#question-analysis-dialog');expect(dialog).to_be_visible();expect(dialog.locator('[data-qa-form]')).to_be_visible()
                        expect(dialog.locator('.ui-source-count strong').nth(0)).to_have_text('2')
                        expect(dialog.locator('.ui-source-count strong').nth(1)).to_have_text('1')
                        assert not dialog.locator('[data-qa-advanced]').evaluate('(element)=>element.open')
                        dialog.locator('button[type=submit]').click();assert not repository.history(case.settings,'123',6,analysis_type='questions')
                        # 修改模型后重新规划，公共下拉保留搜索和原生值。
                        dialog.locator('input[name=allow_paid]').check()
                        dialog.locator('select[name=model]').select_option('deepseek-v4-pro',force=True)
                        expect(dialog.locator('select[name=model]')).to_have_value('deepseek-v4-pro')
                        expect(dialog.locator('button[type=submit]')).to_be_enabled()
                        expect(dialog.locator('input[name=allow_paid]')).not_to_be_checked()
                        assert page.locator('main').count()==1
                        page.screenshot(path=str(output/f'dialog-{width}.png'))
                        dialog.get_by_role('button',name='关闭',exact=True).click()
                        # 记录页也在当前页打开同一套弹窗，预览滚动不带走底部操作。
                        page.goto(base+'/analysis/questions/products/123?batch=6')
                        expect(page.get_by_text('本批尚未分析，点击上方“分析问答”生成报告。')).to_be_visible()
                        page.locator('[data-qa-open]').click();expect(dialog.locator('[data-qa-form]')).to_be_visible()
                        assert page.url==base+'/analysis/questions/products/123?batch=6'
                        dialog.locator('details').first.locator('summary').click()
                        dialog.locator('.ui-confirm-body').evaluate('(element)=>element.scrollTop=element.scrollHeight')
                        expect(dialog.locator('button[type=submit]')).to_be_in_viewport()
                        expect(dialog.get_by_role('button',name='关闭',exact=True)).to_be_in_viewport()
                        page.route('**/api/analysis/questions/products/123',lambda route:route.fulfill(status=400,content_type='application/json',body='{"error":"测试错误：请重新核对计划"}'))
                        dialog.locator('input[name=allow_paid]').check()
                        dialog.locator('button[type=submit]').click()
                        expect(dialog.locator('[data-qa-feedback]')).to_have_text('测试错误：请重新核对计划')
                        expect(dialog.locator('button[type=submit]')).to_be_enabled()
                        page.unroute('**/api/analysis/questions/products/123')
                        page.screenshot(path=str(output/f'history-dialog-{width}.png'))
                        dialog.get_by_role('button',name='取消',exact=True).click();expect(dialog).not_to_be_visible()
                        identifier=case.create_qa();data=service.preview(case.settings,'123',6);client=Mock();client.complete_messages.return_value={'text':sample_report(data['items']),'finish_reason':'stop','usage':{'total_tokens':10}}
                        runtime.execute(case.settings,identifier,Mock(),Mock(return_value=client))
                        page.goto(base+'/analysis/questions/tasks/'+identifier)
                        expect(page.locator('.qa-report-body .ui-report-focus')).to_have_count(1)
                        expect(page.locator('[data-qa-directory] nav a')).to_have_count(7)
                        assert page.locator('[data-qa-directory]').evaluate('(element)=>element.open')==(width>1000)
                        expect(page.get_by_text('测试商品',exact=True)).to_be_visible()
                        evidence=page.locator('.ui-report-evidence');expect(evidence).to_have_count(1);assert evidence.get_attribute('open') is None
                        evidence.locator('summary').click();expect(evidence.locator('.ui-report-evidence-item')).to_be_visible()
                        evidence.locator('[data-qa-source]').click()
                        expect(page.locator('#answer-20')).to_be_visible()
                        page.locator('[data-qa-source-back]').click()
                        expect(evidence.locator('.ui-report-evidence-item')).to_be_in_viewport()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'report-{width}.png'))
                        page.goto(base+'/analysis/questions/products/123?batch=6');expect(page.get_by_text('查看报告与进度')).to_be_visible()
                        assert not errors,errors
                        print('viewport',width,'passed')
                finally:page.close();server.shutdown();thread.join();case.doCleanups()
        finally:browser.close()


if __name__=='__main__':check()
