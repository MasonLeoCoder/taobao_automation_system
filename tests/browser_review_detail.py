"""明细顶部与整批SKU分布的隔离页面验收，禁止真实AI及采集请求。"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright,expect
from ecommerce.modules.review_analysis import service
from ecommerce.infrastructure.storage import analysis_store as repository
from tests.unit.test_review_analysis_v6 import ReviewV6Tests


def seed(case):
    """准备25条含追评/缺失规格的评价、另一批次及成功报告；参数：用例；返回：分析编号。"""
    with case.engine.begin() as conn:
        table=case.tables['review_observations']
        conn.execute(table.update().where(table.c.id==8).values(sku_text='规格A'))
        conn.execute(table.update().where(table.c.id==9).values(sku_text=None))
    for identifier in range(10,33):
        case.put('review_observations',id=identifier,batch_id=5,position=identifier-7,content='脱敏评价'+str(identifier),
                 sku_text='规格A' if identifier<24 else '规格B' if identifier<32 else '',append_reviews=[])
    case.put('collection_batches',id=4,product_id=1,module='review_summary',visibility='published',status='complete',captured_at=datetime(2026,10,28))
    case.put('review_summaries',batch_id=4,count_raw='1万+',positive_rate_percent=99.6,period_raw='近3个月',tags=[])
    case.put('collection_batches',id=6,product_id=1,module='reviews',visibility='published',status='complete',captured_at=datetime(2026,10,30))
    case.put('review_observations',id=40,batch_id=6,position=1,content='另一批脱敏评价',sku_text='另一个规格',append_reviews=[])
    identifier=case.start();case.run_mock(identifier);return identifier


def check(output):
    """检查默认收起、整批口径、翻页/排序/换批、原弹窗及数据只读；参数：目录；返回：无。"""
    output.mkdir(parents=True,exist_ok=True);results=[];errors=[];blocked=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width,height in ((1440,1000),(390,844),(320,844)):
                case=ReviewV6Tests();case.setUp();identifier=seed(case);before=repository.read(case.settings,identifier)
                store=SimpleNamespace(engine=case.engine,tables=case.tables)
                server=make_server('127.0.0.1',0,case.app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                base=f'http://127.0.0.1:{server.server_port}';page=browser.new_page(viewport={'width':width,'height':height})
                page.on('pageerror',lambda error:errors.append(str(error)))
                def guard(route):
                    """隔离采集状态并只允许本机GET；参数：请求；返回：无。"""
                    url=route.request.url
                    if route.request.method!='GET' or not url.startswith(base+'/'):blocked.append(url);route.abort()
                    elif urlsplit(url).path=='/api/collection-jobs/active':route.fulfill(json={'jobs':[]})
                    elif urlsplit(url).path=='/api/collections/products/123/jobs':route.fulfill(json={'jobs':[]})
                    else:route.continue_()
                page.route('**/*',guard)
                try:
                    with patch('ecommerce.infrastructure.storage.collection_browser.collection_store',return_value=store),patch('ecommerce.infrastructure.storage.collection_queries.collection_store',return_value=store),patch.object(service,'create_client',side_effect=AssertionError('查看不得调用AI')),patch.object(service,'launch',side_effect=AssertionError('查看不得派发')):
                        assert page.goto(base+'/collections/products/123?module=reviews&summary_batch=4&reviews_batch=5').status==200
                        comments=page.locator('#review-comments');fold=comments.locator('.review-sku-distribution')
                        expect(fold).not_to_have_attribute('open','');expect(fold.locator('summary')).to_contain_text('共25条评价')
                        expect(comments.locator('.review-batch-result')).to_contain_text('本批已有分析结果')
                        expect(comments.get_by_role('link',name='查看本批报告',exact=True)).to_have_attribute('href','/analysis/reviews/tasks/'+identifier)
                        expect(comments.locator('.module-job-status')).to_have_count(1)
                        toolbar=comments.locator('.review-detail-toolbar');analysis=comments.locator('.review-analysis-panel')
                        assert toolbar.bounding_box()['y']<fold.bounding_box()['y']<analysis.bounding_box()['y']
                        fold.locator('summary').focus();page.keyboard.press('Enter')
                        table=fold.locator('tbody');expect(table.locator('tr')).to_have_count(3)
                        expected=['规格A 15 条 60.0%','规格B 8 条 32.0%','未记录规格 2 条 8.0%']
                        actual=[' '.join(row.inner_text().split()) for row in table.locator('tr').all()];assert actual==expected
                        assert fold.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
                        comments.locator('.review-detail-heading').scroll_into_view_if_needed();page.screenshot(path=str(output/f'detail-open-{width}.png'))
                        fold.locator('summary').click();expect(table).not_to_be_visible()
                        page.screenshot(path=str(output/f'detail-closed-{width}.png'))
                        comments.get_by_role('link',name='下一页',exact=True).click()
                        expect(comments.locator('.review-comment-table tbody tr')).to_have_count(5)
                        expect(fold).not_to_have_attribute('open','');fold.locator('summary').click()
                        assert [' '.join(row.inner_text().split()) for row in fold.locator('tbody tr').all()]==expected
                        comments.get_by_role('link',name='商品规格',exact=False).click()
                        expect(fold).not_to_have_attribute('open','');fold.locator('summary').click()
                        assert [' '.join(row.inner_text().split()) for row in fold.locator('tbody tr').all()]==expected
                        # 尚未应用的选择不能改变当前统计和分析范围。
                        toolbar.locator('select[name=reviews_batch]').select_option('6')
                        expect(fold.locator('summary')).to_contain_text('共25条评价')
                        expect(comments.locator('[data-review-analysis=start]')).to_have_attribute('href','/analysis/reviews/products/123/new?batch=5')
                        with page.expect_navigation():toolbar.get_by_role('button',name='查看',exact=True).click()
                        query=parse_qs(urlsplit(page.url).query);assert query['reviews_batch']==['6'] and query['summary_batch']==['4']
                        expect(fold).not_to_have_attribute('open','');expect(fold.locator('summary')).to_contain_text('共1条评价')
                        fold.locator('summary').click();expect(fold.locator('tbody')).to_contain_text('另一个规格');expect(fold.locator('tbody')).to_contain_text('100.0%')
                        comments.locator('[data-review-analysis=start]').click()
                        form=page.locator('#review-analysis-dialog [data-ra-start]');expect(form).to_be_visible();expect(form).to_have_attribute('data-batch','6')
                        expect(form.locator('[name=allow_paid]')).not_to_be_checked();page.keyboard.press('Escape')
                        assert comments.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
                        assert repository.read(case.settings,identifier)==before
                    results.append({'width':width,'default_closed':True,'batch5_distribution':actual,'all_pages_and_sort_stable':True,
                        'pending_selection_preserved':True,'batch6_count':1,'dialog_batch':6,'existing_report_link':True,'ai_calls':0,'writes':0})
                finally:page.close();server.shutdown();thread.join(5);case.doCleanups()
        finally:browser.close()
    assert not errors and not blocked
    (output/'browser.json').write_text(json.dumps({'checks':results,'page_errors':errors,'blocked_requests':blocked,'real_ai_calls':0,'formal_writes':0},ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS: detail layout, closed whole-batch SKU counts, pagination/sort/batch switch, original report/dialog; AI=0')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);check(parser.parse_args().output)
