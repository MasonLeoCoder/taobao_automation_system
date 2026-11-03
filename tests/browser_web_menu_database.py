"""数据库菜单刷新与状态交互；临时SQL菜单配置，全部业务写入请求被阻断。"""
import json
from pathlib import Path
from threading import Thread
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from urllib.parse import urlsplit
from tests.unit.test_web_menu_database import MenuDatabaseTests
from tests.browser_workspace_plan import Quiet

OUTPUT = Path('workspace/output/reports/web_menu_database/browser')


def check():
    """检查数据库文字、移动、隐藏、停用和恢复；参数：无；返回：保存三种宽度证据。"""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    case = MenuDatabaseTests()
    case.setUp()
    server = make_server('127.0.0.1', 0, case.app, threaded=True, request_handler=Quiet)
    Thread(target=server.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    errors, blocked, results = [], [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for width in (1440, 390, 320):
                case.change('ledger', name='经营账本', group_key='operations', sort_order=40, is_enabled=True, is_visible=True)
                case.change('research', group=True, name='市场与选品', is_visible=True, is_enabled=True)
                context = browser.new_context(viewport=dict(width=width, height=950))

                def guard(route):
                    """仅允许本机读取；参数：请求；返回：无。"""
                    request = route.request
                    if request.method not in ('GET', 'HEAD') or urlsplit(request.url).hostname != '127.0.0.1':
                        blocked.append(request.method+' '+urlsplit(request.url).path)
                        route.abort()
                    else:
                        route.continue_()

                context.route('**/*', guard)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                assert page.goto(base+'/plans/ledger').status == 200
                case.change('ledger', name='收支管理', group_key='research', sort_order=0)
                case.change('research', group=True, name='市场研究')
                page.reload()
                expect(page.get_by_role('heading', name='收支管理', exact=True)).to_be_visible()
                if width <= 700:
                    page.locator('[data-workspace-menu]>summary').click()
                nav = page.locator('.workspace-nav')
                current = nav.locator('[data-nav-group=research]')
                expect(current.locator('[aria-current=page]')).to_have_text('收支管理待建设')
                expect(current.locator('a').first).to_have_attribute('href', '/plans/ledger')
                page.wait_for_function('() => document.documentElement.scrollWidth <= innerWidth+1')
                page.screenshot(path=str(OUTPUT/f'renamed-{width}.png'), full_page=True)
                case.change('ledger', is_visible=False)
                assert page.reload().status == 200
                expect(page.get_by_role('heading', name='收支管理', exact=True)).to_be_visible()
                expect(nav.locator('a[href="/plans/ledger"]')).to_have_count(0)
                case.change('ledger', is_enabled=False)
                assert page.reload().status == 403
                expect(page.get_by_role('heading', name='功能已停用', exact=True)).to_be_visible()
                page.screenshot(path=str(OUTPUT/f'disabled-{width}.png'), full_page=True)
                case.change('ledger', is_enabled=True, is_visible=True)
                case.change('research', group=True, is_enabled=False)
                assert page.reload().status == 403
                case.change('research', group=True, is_enabled=True, is_visible=False)
                assert page.reload().status == 200
                expect(nav.locator('[data-nav-group=research]')).to_have_count(0)
                case.change('research', group=True, is_visible=True)
                assert page.reload().status == 200
                expect(nav.locator('[data-nav-group=research]')).to_have_count(1)
                context.close()
                results.append(dict(width=width, rename=True, regroup=True, hidden_link=True, disabled_link=True, parent=True, restored=True))
            browser.close()
        assert not errors, errors
        assert not blocked, blocked
        result = dict(widths=results, errors=errors, blocked=blocked)
        (OUTPUT/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    finally:
        server.shutdown()
        case.doCleanups()


if __name__ == '__main__':
    check()
