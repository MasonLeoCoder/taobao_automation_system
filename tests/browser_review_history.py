"""评价分析记录布局及筛选交互，只用临时数据库和模拟AI。"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import threading
from unittest.mock import patch
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright,expect
from ecommerce.modules.review_analysis import service
from ecommerce.infrastructure.storage import analysis_store as repository
from tests.unit.test_review_analysis_v6 import ReviewV6Tests


def seed(case):
    """创建成功、部分、失败、活动和空批次场景；参数：临时用例；返回：任务编号。"""
    complete=case.start();case.run_mock(complete)
    partial=case.start(force=True);case.run_mock(partial,finish='length')
    failed=case.start(force=True);repository.update_task(case.settings,failed,status='failed',active_key=None,message='脱敏失败')
    running=case.start(force=True);repository.update_task(case.settings,running,status='running',owner={'pid':42,'started_at':'test-only'})
    with case.engine.begin() as conn:
        for identifier,minute in ((complete,0),(partial,1),(running,2),(failed,3)):
            table=case.tables['analysis_tasks'];conn.execute(table.update().where(table.c.id==identifier).values(created_at=datetime(2026,10,31,11,minute,5,987654)))
    case.put('collection_batches',id=6,product_id=1,module='reviews',visibility='published',status='complete',captured_at=datetime(2026,10,30,10,0,0,123456))
    case.put('review_observations',id=10,batch_id=6,position=1,content='另一批脱敏文字',sku_text='测试规格',append_reviews=[])
    return complete,partial,failed,running


def check(output):
    """检查桌面手机布局、应用范围、原弹窗和无脚本回退；参数：目录；返回：无。"""
    output.mkdir(parents=True,exist_ok=True);results=[];errors=[];external=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width,height in ((1440,1000),(390,844)):
                case=ReviewV6Tests();case.setUp();complete,partial,failed,running=seed(case)
                before={identifier:repository.read(case.settings,identifier) for identifier in (complete,partial,failed,running)}
                server=make_server('127.0.0.1',0,case.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base=f'http://127.0.0.1:{server.server_port}';page=browser.new_page(viewport={'width':width,'height':height})
                page.on('pageerror',lambda error:errors.append(str(error)))
                def guard(route):
                    """只放行隔离服务；参数：请求；返回：无。"""
                    if route.request.url.startswith(base+'/'):route.continue_()
                    else:external.append(route.request.url);route.abort()
                page.route('**/*',guard)
                try:
                    with patch.object(service,'owner_alive',return_value=True),patch.object(service,'create_client',side_effect=AssertionError('查看不得调用AI')),patch.object(service,'launch',side_effect=AssertionError('查看不得派发')):
                        assert page.goto(base+'/analysis/reviews/products/123?batch=5').status==200
                        toolbar=page.locator('.ra-history-toolbar');trigger=toolbar.locator('.ss-trigger')
                        apply=toolbar.get_by_role('button',name='查看',exact=True);start=toolbar.get_by_role('link',name='分析评价',exact=True)
                        expect(page.locator('.ra-history-latest a')).to_have_attribute('href','/analysis/reviews/tasks/'+complete)
                        expect(page.get_by_text('最新任务失败。',exact=False)).to_be_visible()
                        expect(page.get_by_text('本批正在分析',exact=False)).to_be_visible()
                        expect(page.locator('.ra-history-table tbody tr')).to_have_count(4)
                        expect(page.locator('.ui-report-state.is-ready')).to_have_text('已完成')
                        expect(page.locator('.ui-report-state.is-error')).to_have_text('失败')
                        expect(page.locator('.ui-report-state.is-active')).to_have_text('执行中')
                        expect(page.locator('.ui-report-state.is-pending')).to_have_text('部分完成')
                        assert '987654' not in page.locator('.ra-history-page').inner_text()
                        expect(page.locator('.ra-history-table td').first).to_have_text('2026-10-31 11:03:05')
                        bounds=[item.bounding_box() for item in (trigger,apply,start)]
                        if width>700:
                            assert max(b['y'] for b in bounds)-min(b['y'] for b in bounds)<=1
                            assert bounds[0]['width']<=420 and bounds[2]['x']>bounds[1]['x']+bounds[1]['width']
                        else:
                            assert abs(bounds[1]['y']-bounds[2]['y'])<=1 and bounds[0]['y']<bounds[1]['y']
                            wrap=page.locator('.ra-history-table-wrap');assert wrap.evaluate('(n)=>n.scrollWidth>n.clientWidth')
                            wrap.evaluate('(n)=>n.scrollLeft=n.scrollWidth');assert wrap.evaluate('(n)=>n.scrollLeft>0')
                            wrap.evaluate('(n)=>n.scrollLeft=0')
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        page.screenshot(path=str(output/f'history-{width}.png'),full_page=True)
                        trigger.click();page.get_by_role('combobox',name='查找记录范围').fill('全部')
                        page.get_by_role('option',name='全部批次',exact=True).click()
                        # 下拉变化不改变已应用范围，直到点击查看。
                        expect(start).to_have_attribute('href','/analysis/reviews/products/123/new?batch=5')
                        expect(page.locator('.ra-history-heading')).to_contain_text('当前批次 5')
                        with page.expect_navigation():apply.click()
                        expect(page).to_have_url(base+'/analysis/reviews/products/123?batch=all')
                        expect(page.locator('[data-review-analysis=start]')).to_have_count(0)
                        expect(page.get_by_text('所选范围中有任务正在分析',exact=False)).to_be_visible()
                        trigger=page.locator('.ra-history-toolbar .ss-trigger');trigger.click()
                        page.get_by_role('option',name='批次 6',exact=False).click()
                        with page.expect_navigation():page.locator('.ra-history-toolbar').get_by_role('button',name='查看',exact=True).click()
                        expect(page).to_have_url(base+'/analysis/reviews/products/123?batch=6')
                        expect(page.get_by_text('此范围尚无分析记录。',exact=True)).to_be_visible()
                        expect(page.locator('.ra-history-latest')).to_have_count(0)
                        page.locator('[data-review-analysis=start]').click()
                        form=page.locator('#review-analysis-dialog [data-ra-start]');expect(form).to_be_visible()
                        expect(form).to_have_attribute('data-batch','6')
                        expect(form.locator('[name=allow_paid]')).not_to_be_checked()
                        page.screenshot(path=str(output/f'dialog-{width}.png'))
                        page.keyboard.press('Escape');expect(page.locator('#review-analysis-dialog')).not_to_be_visible()
                        back=page.get_by_role('link',name='返回评价明细',exact=True)
                        assert 'reviews_batch=6' in back.get_attribute('href')
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        assert all(repository.read(case.settings,identifier)==snapshot for identifier,snapshot in before.items())
                    results.append({'width':width,'aligned_controls':True,'scope_applies_on_submit':True,'prior_success_preserved':True,
                        'states_and_seconds':True,'empty_batch':True,'dialog_scope':6,'table_scroll_contained':True,'ai_calls':0,'writes':0})
                finally:page.close();server.shutdown();thread.join(5);case.doCleanups()
            # 禁用脚本后保留原生选择及GET提交。
            case=ReviewV6Tests();case.setUp()
            server=make_server('127.0.0.1',0,case.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            page=browser.new_page(java_script_enabled=False)
            try:
                with patch.object(service,'create_client',side_effect=AssertionError('原生筛选不得调用AI')):
                    page.goto(f'http://127.0.0.1:{server.server_port}/analysis/reviews/products/123?batch=5')
                    page.get_by_label('记录范围',exact=True).select_option('all')
                    with page.expect_navigation():page.get_by_role('button',name='查看',exact=True).click()
                    assert page.url.endswith('?batch=all')
            finally:page.close();server.shutdown();thread.join(5);case.doCleanups()
        finally:browser.close()
    assert not errors and not external
    (output/'browser.json').write_text(json.dumps({'checks':results,'native_select':True,'page_errors':errors,'external_requests':external,'real_ai_calls':0,'formal_writes':0},ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: desktop/mobile history layout, filters, states, prior report, dialog, native select; AI=0')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);check(parser.parse_args().output)
