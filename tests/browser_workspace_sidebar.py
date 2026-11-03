"""侧栏动画与布局检查；使用临时浏览器，仅允许本机只读请求。"""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app
from tests.browser_workspace_plan import Quiet

OUTPUT = Path('workspace/output/reports/workspace_sidebar')


def settled(page):
    """等待侧栏自身动画收尾；参数：页面；返回：无。"""
    page.wait_for_function("() => !document.querySelector('[data-workspace-menu]').getAnimations({subtree:true}).some(a => a.playState === 'running')")


def check():
    """验证插值、打断、键盘、记忆、减少动画及五种布局；参数：--real读取8000端口。"""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    real = '--real' in sys.argv
    mode = 'real' if real else 'isolated'
    temp = TemporaryDirectory()
    server = None
    if not real:
        server = make_server('127.0.0.1', 0, create_app(load_settings(root=Path(temp.name))),
                             threaded=True, request_handler=Quiet)
        Thread(target=server.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(8000 if real else server.server_port)
    errors = []
    blocked = []

    def guard(route):
        """阻断外站与业务写入；参数：请求；返回：无。"""
        request = route.request
        if request.method not in ('GET', 'HEAD') or urlsplit(request.url).hostname != '127.0.0.1':
            blocked.append(request.method + ' ' + urlsplit(request.url).path)
            route.abort()
        else:
            route.continue_()

    def new_page(browser, **options):
        """创建隔离且受保护的页面；参数：浏览器、上下文选项；返回：上下文及页面。"""
        context = browser.new_context(**options)
        context.route('**/*', guard)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        assert page.goto(base + '/plans/ledger').status == 200
        return context, page

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context, page = new_page(browser, viewport=dict(width=1440, height=900))
            nav = page.get_by_role('navigation', name='主导航', exact=True)
            expect(nav.locator('.workspace-icon')).to_have_count(6)
            # 真实逐帧高度必须存在中间值，不把箭头旋转当展开动画。
            samples = page.evaluate("""async () => {
              const group = document.querySelector('[data-nav-group=research]');
              const panel = group.querySelector('.workspace-group-panel');
              group.querySelector('summary').click();
              const samples = [];
              for (let i=0;i<24;i++) {
                await new Promise(requestAnimationFrame);
                samples.push(panel.getBoundingClientRect().height);
              }
              return samples;
            }""")
            settled(page)
            assert len({round(height) for height in samples}) > 3, samples
            assert any(0 < height < max(samples) - 1 for height in samples), samples
            # 连续反转时从当前帧衔接，最终收起，内容不能继续获得焦点。
            state = page.evaluate("""async () => {
              const group = document.querySelector('[data-nav-group=research]');
              const panel = group.querySelector('.workspace-group-panel');
              const summary = group.querySelector('summary');
              summary.click();
              await new Promise(r => setTimeout(r, 50));
              const before = panel.getBoundingClientRect().height;
              summary.click();
              const after = panel.getBoundingClientRect().height;
              await new Promise(r => setTimeout(r, 35)); summary.click();
              await new Promise(r => setTimeout(r, 25)); summary.click();
              await new Promise(r => setTimeout(r, 25)); summary.click();
              return {before, after, target:group.dataset.expanded, inert:panel.inert};
            }""")
            assert abs(state['before'] - state['after']) < 2, state
            assert state['target'] == 'false' and state['inert'], state
            settled(page)
            expect(nav.locator('[data-nav-group=research]')).not_to_have_attribute('open', '')
            # 当前分组收起后仍能识别当前子页面；键盘Enter/Space可切换。
            current = nav.locator('[data-nav-group=operations]')
            summary = current.locator(':scope > summary')
            summary.focus()
            summary.press('Enter')
            settled(page)
            expect(current.locator('.workspace-current-location')).to_be_visible()
            expect(summary).to_have_attribute('aria-expanded', 'false')
            summary.press('Space')
            settled(page)
            expect(summary).to_have_attribute('aria-expanded', 'true')
            # 非当前组记忆与刷新后当前组展开保持原行为。
            nav.locator('[data-nav-group=products]>summary').click()
            settled(page)
            page.reload()
            expect(nav.locator('[data-nav-group=products]')).to_have_attribute('open', '')
            expect(nav.locator('[data-nav-group=operations]')).to_have_attribute('open', '')
            # 多组展开时只有侧栏内容滚动，品牌位置固定。
            page.evaluate("document.querySelectorAll('[data-nav-group]').forEach(g => {if(g.dataset.expanded!=='true')g.querySelector('summary').click()})")
            settled(page)
            scroll = page.evaluate("""() => {
              const menu=document.querySelector('[data-workspace-menu]');
              const brand=document.querySelector('.brand'); const top=brand.getBoundingClientRect().top;
              menu.scrollTop=menu.scrollHeight;
              return {scroll:menu.scrollTop,brandShift:brand.getBoundingClientRect().top-top};
            }""")
            assert scroll['scroll'] > 0 and scroll['brandShift'] == 0, scroll
            context.close()
            widths = []
            for width in (1440, 1024, 768, 390, 320):
                context, page = new_page(browser, viewport=dict(width=width, height=900))
                menu = page.locator('[data-workspace-menu]')
                if width <= 700:
                    menu.locator(':scope > summary').click()
                    settled(page)
                page.wait_for_function('() => document.documentElement.scrollWidth <= innerWidth + 1')
                page.screenshot(path=str(OUTPUT / f'{mode}-{width}.png'), full_page=True)
                if width <= 700:
                    menu.locator('a[href="/plans/ledger"]').click()
                    expect(page.get_by_role('heading', name='经营账本', exact=True)).to_be_visible()
                else:
                    menu.locator('[data-nav-group=operations]>summary').click()
                    settled(page)
                    page.screenshot(path=str(OUTPUT / f'{mode}-collapsed-{width}.png'), full_page=True)
                widths.append(width)
                context.close()
            # 减少动态效果直接切换，不保留运行中的动画。
            context, page = new_page(browser, reduced_motion='reduce')
            page.locator('[data-nav-group=research]>summary').click()
            assert page.evaluate("document.querySelector('[data-workspace-menu]').getAnimations({subtree:true}).length") == 0
            context.close()
            # 存储被禁用时菜单仍可操作，脚本被禁用时保留原生details。
            for no_js in (False, True):
                context = browser.new_context(java_script_enabled=not no_js)
                context.route('**/*', guard)
                if not no_js:
                    context.add_init_script("Storage.prototype.getItem=Storage.prototype.setItem=()=>{throw new Error('disabled')}")
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/plans/ledger')
                summary = page.locator('[data-nav-group=research]>summary')
                summary.click()
                if not no_js:
                    settled(page)
                expect(page.locator('[data-nav-group=research]')).to_have_attribute('open', '')
                context.close()
            browser.close()
            assert not errors, errors
            assert not blocked, blocked
            result = dict(mode=mode, widths=widths, animation_frames=samples, rapid_reverse=state,
                          keyboard=True, memory=True, reduced_motion=True, no_storage=True,
                          no_javascript=True, sidebar_scroll=scroll, errors=errors, blocked=blocked)
            (OUTPUT / f'{mode}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False))
    finally:
        if server:
            server.shutdown()
        temp.cleanup()


if __name__ == '__main__':
    check()
