"""公共折叠区的颜色回归，仅响应静态样例，不访问数据库或外部服务。"""
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def check():
    """检查链接按钮、普通链接及禁用状态在折叠/报告中的颜色；参数：无；返回：断言。"""
    static=Path(__file__).resolve().parents[1]/'src/ecommerce/web/static'
    output=Path('workspace/output/reports/fold_buttons/isolated')
    output.mkdir(parents=True,exist_ok=True)
    controls='''<details class="ui-report-fold"><summary>展开测试控件</summary>
      <div class="ui-report-fold-body">
      <a href="#target" class="button action-primary">查看资料</a>
      <a href="#target" class="ui-button ui-primary">公共主按钮</a>
      <a href="#target" class="button action-neutral">查看记录</a>
      <a href="#target" class="button action-history">查看日志</a>
      <a href="#target" class="button disabled" aria-disabled="true">暂不可用</a>
      <a href="#target" class="plain-link">普通来源链接</a>
      </div></details>'''
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <link rel="stylesheet" href="/static/form_controls.css">
      <body>'''+controls+'<article class="ui-report-paper">'+controls+'</article></body></html>'
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            for width in (1440,390,320):
                page=browser.new_page(viewport={'width':width,'height':1000})
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))

                def respond(route):
                    """只提供固定页面和公共样式；参数：浏览器路由；返回：静态响应。"""
                    url=urlsplit(route.request.url)
                    assert route.request.method=='GET' and url.hostname=='127.0.0.1'
                    if url.path=='/':route.fulfill(content_type='text/html',body=html)
                    elif url.path=='/static/form_controls.css':
                        route.fulfill(content_type='text/css',body=(static/'form_controls.css').read_text(encoding='utf-8'))
                    else:route.abort()

                page.route('**/*',respond)
                page.goto('http://127.0.0.1:8000/')
                for summary in page.locator('summary').all():summary.click()
                for control in page.locator('a').all():
                    # 取公共变量定义的预期文字色，避免误把“存在文字”当作显示正常。
                    expected=control.evaluate('''element => {
                      const probe=document.createElement('span');
                      probe.style.color=element.classList.contains('plain-link') ? 'var(--control-accent)' :
                        element.classList.contains('disabled') ? 'var(--control-disabled-text)' : 'var(--action-text,#fff)';
                      element.append(probe);const color=getComputedStyle(probe).color;probe.remove();return color;
                    }''')
                    expect(control).to_have_css('color',expected)
                    control.hover();expect(control).to_have_css('color',expected)
                    assert control.text_content().strip()
                page.mouse.move(0,0)
                page.locator('summary').first.focus();page.keyboard.press('Tab')
                expect(page.locator('.button.action-primary:focus-visible')).to_have_count(1)
                expect(page.locator('.button.action-primary:focus-visible')).to_have_css('color','rgb(255, 255, 255)')
                page.screenshot(path=str(output/f'controls-{width}.png'),full_page=True)
                assert not errors,errors
                page.close();print(width,'passed')
        finally:browser.close()


if __name__=='__main__':check()
