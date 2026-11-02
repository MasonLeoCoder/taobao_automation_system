"""综合分析桌面与手机交互，隔离SQLite/模拟AI；真实模式仅GET。"""
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch, Mock
from types import SimpleNamespace
import sys
from playwright.sync_api import sync_playwright, expect
from tests.unit import test_composite_analysis as fixtures
from ecommerce.modules.composite_analysis import runtime

OUTPUT = Path('workspace/output/reports/composite_analysis')


def check():
    """验证来源弹窗、费用守卫、报告阅读与手机排版；参数：无；返回：断言。"""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for width in (1440, 390, 320):
            context = browser.new_context(viewport={'width':width,'height':1000}); page = context.new_page(); errors = []; posts = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            case = None; patches = []
            if '--real' not in sys.argv:
                case = fixtures.CompositeTests(); case.setUp()
                for target in ('ecommerce.infrastructure.storage.collection_browser.collection_store','ecommerce.infrastructure.storage.collection_queries.collection_store'):
                    patched = patch(target, return_value=SimpleNamespace(engine=case.engine,tables=case.tables)); patched.start(); patches.append(patched)
                patched = patch('ecommerce.modules.composite_analysis.service.launch'); patched.start(); patches.append(patched)
            def route(route):
                """所有真实请求只读；隔离创建使用模拟后台，不连接服务商。"""
                r=route.request; url=urlsplit(r.url)
                if url.hostname!='127.0.0.1': route.abort(); return
                if not case:
                    if r.method not in ('GET','HEAD'): raise AssertionError('真实POST被阻止')
                    route.continue_(); return
                if url.path=='/static/collection.js': route.fulfill(content_type='text/javascript',body=''); return
                if url.path.startswith('/api/collections/'):
                    route.fulfill(content_type='application/json',body='{"jobs":[]}'); return
                response=case.client.open(url.path+('?' +url.query if url.query else ''),method=r.method,data=r.post_data,
                    base_url='http://127.0.0.1:18765',content_type=r.headers.get('content-type'),headers={k:v for k,v in r.headers.items() if k in ('x-collection-token','origin')})
                if r.method=='POST':
                    posts.append(url.path)
                    if response.status_code==200 and url.path.endswith('/products/123') and response.json.get('created'):
                        client=Mock(); client.complete_messages.return_value={'text':fixtures.sample_combined(),'finish_reason':'stop','usage':{'prompt_tokens':100,'completion_tokens':200,'total_tokens':300}}
                        runtime.execute(case.settings,response.json['id'],Mock(),Mock(return_value=client))
                route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
            try:
                context.route('**/*',route)
                base='http://127.0.0.1:8000' if not case else 'http://127.0.0.1:18765'; product='123' if case else '1050158363561'
                response=page.goto(base+'/collections/products/'+product+'?module=composite'); assert response.status==200
                expect(page.get_by_role('heading',name='综合选品分析',exact=True)).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path=str(OUTPUT/f'{"isolated" if case else "real"}-panel-{width}.png'),full_page=True)
                if not page.locator('[data-ca-open]').count():
                    assert not case; assert not errors,errors; continue
                page.locator('[data-ca-open]').click(); expect(page.locator('[data-ca-form]')).to_be_visible(timeout=45000)
                expect(page.locator('[name=allow_paid]')).not_to_be_checked()
                dialog=page.locator('#composite-analysis-dialog'); assert dialog.bounding_box()['width']<=width
                page.screenshot(path=str(OUTPUT/f'{"isolated" if case else "real"}-dialog-{width}.png'),full_page=True)
                if case:
                    page.locator('[data-ca-form] [type=submit]').click(); assert not posts
                    page.locator('[name=allow_paid]').check()
                    page.locator('select[name=model] + .ss-trigger').click()
                    page.get_by_role('option',name='deepseek-v4-pro',exact=True).click()
                    expect(page.locator('select[name=model]')).to_have_value('deepseek-v4-pro')
                    expect(page.locator('[name=allow_paid]')).not_to_be_checked()
                    expect(page.locator('[data-ca-form] [type=submit]')).to_be_enabled()
                    # 再次打开与关闭模型选择，防止旧弹窗控件只响应一次的回归。
                    page.locator('select[name=model] + .ss-trigger').click();page.keyboard.press('Escape')
                    page.locator('[name=allow_paid]').check();page.locator('[data-ca-form] [type=submit]').click()
                    page.wait_for_url('**/analysis/composite/tasks/*')
                    expect(page.get_by_role('heading',name='综合选品报告',exact=True)).to_be_visible()
                    fold=page.locator('.ui-report-evidence').first;assert not fold.evaluate('(e)=>e.open')
                    fold.locator('summary').click();expect(fold.locator('.ui-report-evidence-item')).to_have_count(2)
                    fold.locator('a').first.click();expect(page.locator('#composite-citation-1')).to_have_attribute('open','')
                    page.screenshot(path=str(OUTPUT/f'isolated-report-{width}.png'),full_page=True)
                    page.locator('[data-ca-check]').click();page.wait_for_url('**/analysis/composite/tasks/*')
                    expect(page.get_by_role('heading',name='综合选品报告',exact=True)).to_be_visible()
                    page.goto(base+'/collections/products/123?module=composite')
                    expect(page.get_by_role('heading',name='最近完成的综合报告')).to_be_visible()
                    page.locator('[data-ca-open]').click()
                    page.locator('select[name=model] + .ss-trigger').click();page.get_by_role('option',name='deepseek-v4-pro',exact=True).click()
                    expect(page.locator('[data-ca-form] [type=submit]')).to_be_enabled()
                    page.locator('[name=allow_paid]').check();page.locator('[data-ca-form] [type=submit]').click()
                    expect(page.locator('[data-ca-choice]')).to_be_visible()
                    page.locator('[data-ca-cancel]').click();expect(dialog).not_to_be_visible()
                else:
                    page.locator('[data-ca-cancel]').click();expect(dialog).not_to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                assert not errors,errors
                print('isolated' if case else 'real',width,'passed; real AI 0; POST',len(posts))
            except Exception:
                print('diagnostic',width,page.url,errors,posts,page.locator('[data-ca-feedback]').all_text_contents(),flush=True)
                page.screenshot(path=str(OUTPUT/f'failure-{width}.png'),full_page=True)
                raise
            finally:
                context.close()
                for patched in reversed(patches):patched.stop()
                if case:case.doCleanups()
        browser.close()


if __name__=='__main__':check()
