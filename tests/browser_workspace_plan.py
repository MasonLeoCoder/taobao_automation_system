"""工作台菜单与规划页只读浏览器检查；外站与写入请求全部阻断。"""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit
from werkzeug.serving import make_server, WSGIRequestHandler
from playwright.sync_api import sync_playwright, expect
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app
from ecommerce.web.workspace_plan import ITEMS

OUTPUT = Path('workspace/output/reports/workspace_plan')


class Quiet(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        """不输出普通只读请求日志；参数：日志；返回：无。"""


def check():
    """检查规划页、原菜单、折叠记忆和五种宽度；参数：--real使用正式只读服务。"""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    real = '--real' in sys.argv
    temp = TemporaryDirectory()
    server = None
    if not real:
        app = create_app(load_settings(root=Path(temp.name)))
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=Quiet)
        Thread(target=server.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(8000 if real else server.server_port)
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport=dict(width=1440, height=1000))
            errors = []
            blocked = []

            def guard(route):
                """仅允许本机GET/HEAD，防止检查触发采集、付费或写入。"""
                request = route.request
                if request.method not in ('GET', 'HEAD') or urlsplit(request.url).hostname != '127.0.0.1':
                    blocked.append(request.method + ' ' + urlsplit(request.url).path)
                    route.abort()
                    return
                route.continue_()

            context.route('**/*', guard)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            # 同一个浏览器中检查记忆、当前组强制展开及所有规划入口。
            assert page.goto(base + '/plans/ledger').status == 200
            nav = page.get_by_role('navigation', name='主导航', exact=True)
            expect(nav.locator('[data-nav-group]')).to_have_count(6)
            expect(nav.locator('[aria-current=page]')).to_have_text('经营账本待建设')
            group = nav.locator('[data-nav-group=products]')
            group.locator('summary').click()
            expect(group).to_have_attribute('open', '')
            page.reload()
            expect(nav.locator('[data-nav-group=products]')).to_have_attribute('open', '')
            nav.locator('[data-nav-group=products]>summary').click()
            page.reload()
            expect(nav.locator('[data-nav-group=products]')).not_to_have_attribute('open', '')
            nav.locator('[data-nav-group=operations]>summary').click()
            page.reload()
            expect(nav.locator('[data-nav-group=operations]')).to_have_attribute('open', '')
            for item in ITEMS.values():
                if item['planned']:
                    assert page.goto(base + '/plans/' + item['key']).status == 200
                    expect(page.get_by_role('heading', name=item['label'], exact=True)).to_be_visible()
                    expect(page.locator('main form')).to_have_count(0)
                    expect(nav.locator('[aria-current=page]')).to_have_count(1)
            assert page.goto(base + '/plans/not-a-plan').status == 404
            # 桌面、平板、手机；用三类代表页检查共享布局不横向溢出。
            for width in (1440, 1024, 768, 390, 320):
                page.set_viewport_size(dict(width=width, height=1000))
                for item in ITEMS.values():
                    if item['key'] in ('ledger', 'listing', 'selection-projects'):
                        assert page.goto(base + '/plans/' + item['key']).status == 200
                        page.wait_for_function('() => document.documentElement.scrollWidth <= innerWidth + 1')
                page.goto(base + '/plans/ledger')
                if width <= 700:
                    menu = page.locator('[data-workspace-menu]')
                    expect(menu).not_to_have_attribute('open', '')
                    menu.locator(':scope > summary').click()
                    expect(nav.locator('a[href="/plans/ledger"]')).to_be_visible()
                    nav.locator('a[href="/plans/ledger"]').click()
                    expect(page.get_by_role('heading', name='经营账本', exact=True)).to_be_visible()
                page.screenshot(path=str(OUTPUT / f'{"real" if real else "isolated"}-ledger-{width}.png'), full_page=True)
                results.append(dict(width=width, checked_pages=['ledger', 'listing', 'selection-projects'], overflow=False))
            # 原入口只读回归；正式库才有商品库、搜索词等可查询数据。
            page.set_viewport_size(dict(width=1440, height=1000))
            original = ('/', '/modules/collection', '/tools', '/tasks', '/logs', '/settings/ai')
            if real:
                original += ('/collections/searches', '/collections/details', '/analysis/searches', '/favorites',
                             '/collections/products/1050158363561?module=composite')
            for path in original:
                assert page.goto(base + path).status == 200, path
                expect(nav.locator('[aria-current=page]')).to_have_count(1)
            # 禁用脚本仍保留原生菜单和已有功能入口。
            no_js = browser.new_context(java_script_enabled=False)
            no_js.route('**/*', guard)
            fallback = no_js.new_page()
            fallback.goto(base + '/plans/ledger')
            expect(fallback.locator('[data-nav-group=operations]')).to_have_attribute('open', '')
            expect(fallback.locator('nav a[href="/plans/ledger"]')).to_be_visible()
            no_js.close()
            assert not errors, errors
            assert not blocked, blocked
            context.close()
            browser.close()
            result = dict(mode='real' if real else 'isolated', viewports=results,
                          errors=errors, blocked=blocked, original_pages=list(original))
            (OUTPUT / f'{result["mode"]}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False))
    finally:
        if server:
            server.shutdown()
        temp.cleanup()


if __name__ == '__main__':
    check()
