"""问答弹窗桌面/手机隔离验收，模拟模型，不发起真实AI或采集。"""
import threading
import json
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
                        case.put('collection_batches',id=4,product_id=1,module='questions',visibility='published',status='complete')
                        case.put('question_observations',id=12,batch_id=4,position=1,question='另一个批次的问题',answers_complete=True)
                        with case.engine.begin() as conn:
                            for task in ('a'*32,'c'*32):conn.execute(case.tables['tasks'].insert().values(id=task,name='taobao.collect',status='succeeded'))
                            table=case.tables['collection_batches']
                            conn.execute(table.update().where(table.c.id==6).values(task_id='a'*32))
                            conn.execute(table.update().where(table.c.id==4).values(task_id='c'*32))
                        job={'id':'b'*32,'product_id':'123','module':'questions','state':'succeeded','done':True,'valid':True,'mode':'single','batch_id':6,'result_url':base+'/collections/products/123?module=questions&batch=6','result_label':'查看资料','logs_url':base+'/logs','message':'本模块已保存并确认入库'}
                        page.route('**/api/collections/products/123/jobs',lambda route:route.fulfill(content_type='application/json',body=json.dumps({'jobs':[job],'plan':{'modules':[]}})))
                        page.goto(base+'/collections/products/123?module=questions&batch=6')
                        expect(page.locator('[data-qa-open]')).to_have_text('分析问答')
                        expect(page.locator('[data-question-page] .module-job-status')).to_have_text('采集完成')
                        expect(page.locator('[data-question-page] .module-job-status')).to_have_count(1)
                        expect(page.get_by_role('button',name='重新采集问大家',exact=True)).to_be_visible()
                        expect(page.get_by_role('link',name='查看采集任务',exact=True)).to_be_visible()
                        expect(page.get_by_role('link',name='查看采集日志',exact=True)).to_be_visible()
                        assert 'a'*32 in page.locator('[data-collection-actions] [data-job-logs]').get_attribute('href')
                        expect(page.get_by_text('更多操作',exact=True)).to_have_count(0)
                        expect(page.locator('.qa-analysis-actions [data-job-result]')).to_have_count(0)
                        page.locator('[data-qa-batch-form] select').select_option('4',force=True)
                        expect(page.locator('[data-qa-batch-pending]')).to_be_visible()
                        expect(page.locator('[data-qa-open]')).to_have_attribute('href','/analysis/questions/products/123/new?batch=6')
                        assert 'a'*32 in page.locator('[data-collection-actions] [data-job-logs]').get_attribute('href')
                        page.locator('[data-qa-batch-form] button[type=submit]').click()
                        expect(page.locator('[data-question-page]')).to_have_attribute('data-applied-batch','4')
                        expect(page.locator('.qa-card')).to_have_count(1)
                        assert 'c'*32 in page.locator('[data-collection-actions] [data-job-logs]').get_attribute('href')
                        page.goto(base+'/collections/products/123?module=questions&batch=6')
                        expect(page.locator('[data-qa-open]')).to_have_text('分析问答')
                        with page.expect_navigation():page.locator('[data-qa-sort-form] select').select_option('content',force=True)
                        assert 'batch=6' in page.url and 'sort=content' in page.url
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.locator('[data-question-page]').screenshot(path=str(output/f'workspace-{width}.png'))
                        job.update(state='failed',valid=False,message='模拟异常：请核对采集日志，原资料保留')
                        page.reload()
                        expect(page.locator('.qa-page-heading .module-job-status')).to_contain_text('模拟异常：请核对采集日志，原资料保留')
                        expect(page.locator('.qa-page-heading .module-job-status')).to_have_count(1)
                        assert 'a'*32 in page.locator('[data-collection-actions] [data-job-logs]').get_attribute('href')
                        job.update(state='succeeded',valid=True,message='本模块已保存并确认入库')
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
                        historyFilter=page.locator('[data-qa-history-filter]')
                        historyFilter.locator('select').select_option('all',force=True)
                        expect(page.locator('[data-qa-history-pending]')).to_be_visible()
                        expect(page.locator('[data-qa-open]')).to_have_attribute('href','/analysis/questions/products/123/new?batch=6')
                        historyFilter.locator('button[type=submit]').click()
                        expect(page.locator('[data-qa-open]')).to_have_count(0)
                        expect(page.locator('.ra-history-heading')).to_contain_text('全部批次')
                        historyFilter=page.locator('[data-qa-history-filter]');historyFilter.locator('select').select_option('6',force=True);historyFilter.locator('button[type=submit]').click()
                        identifier=case.create_qa();data=service.preview(case.settings,'123',6);client=Mock()
                        text=sample_report(data['items']).replace('## 购买顾虑与决策因素\n\n脱敏测试内容。','## 购买顾虑与决策因素\n\n实际提及的因素：①黏性（问题10）；②规格，尚未核实（问题11）；③价格，缺少报价。\n\n区分：性能未验证；规格信息缺失。')
                        text=text.replace('\n\n【问题','\n\n依据原文：\n\n【问题')
                        client.complete_messages.return_value={'text':text,'finish_reason':'stop','usage':{'total_tokens':10}}
                        runtime.execute(case.settings,identifier,Mock(),Mock(return_value=client))
                        page.goto(base+'/analysis/questions/tasks/'+identifier)
                        expect(page.locator('.qa-report-body .ui-report-focus')).to_have_count(1)
                        expect(page.locator('[data-qa-directory] nav a')).to_have_count(7)
                        assert page.locator('[data-qa-directory]').evaluate('(element)=>element.open')==(width>1000)
                        expect(page.get_by_text('测试商品',exact=True)).to_be_visible()
                        expect(page.locator('#qa-section-3 .qa-numbered-list>li')).to_have_count(3)
                        expect(page.get_by_text('依据原文：',exact=True)).to_have_count(0)
                        page.locator('#qa-section-3').screenshot(path=str(output/f'paragraph-{width}.png'))
                        evidence=page.locator('.ui-report-evidence');expect(evidence).to_have_count(1);assert evidence.get_attribute('open') is None
                        evidence.locator('summary').click();expect(evidence.locator('.ui-report-evidence-item')).to_be_visible()
                        evidence.locator('[data-qa-source]').click()
                        expect(page.locator('#answer-20')).to_be_visible()
                        page.locator('[data-qa-source-back]').click()
                        expect(evidence.locator('.ui-report-evidence-item')).to_be_in_viewport()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'report-{width}.png'))
                        page.goto(base+'/analysis/questions/products/123?batch=6');expect(page.locator('.ra-history-table').get_by_role('link',name='查看报告',exact=True)).to_be_visible()
                        expect(page.locator('.ra-history-latest')).to_contain_text('报告保存于')
                        page.screenshot(path=str(output/f'history-{width}.png'))
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        with patch.object(service,'launch'):
                            failed=service.start(case.settings,'123',batch_id=6,model=data['model'],input_hash=data['input_hash'],plan_hash=data['plan_hash'],allow_paid=True,force=True)['id']
                        repository.update_task(case.settings,failed,status='failed',active_key=None,message='脱敏模拟：上次请求未完成')
                        page.reload();expect(page.locator('.ra-history-latest').get_by_role('link',name='查看报告',exact=True)).to_have_attribute('href','/analysis/questions/tasks/'+identifier)
                        expect(page.locator('.qa-history-reason')).to_have_text('脱敏模拟：上次请求未完成')
                        expect(page.locator('.ra-history-table').get_by_role('link',name='查看进度',exact=True)).to_have_attribute('href','/analysis/questions/tasks/'+failed)
                        assert not errors,errors
                        print('viewport',width,'passed')
                finally:page.close();server.shutdown();thread.join();case.doCleanups()
        finally:browser.close()


if __name__=='__main__':check()
