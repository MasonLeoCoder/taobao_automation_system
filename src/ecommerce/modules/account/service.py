"""账号业务流程，复用当前任务的浏览器上下文。"""
from ecommerce.infrastructure.browser.session import save_session
from ecommerce.platforms.qianniu.session import browser_session
from ecommerce.platforms.qianniu.login import check_login, login_with_qrcode
from ecommerce.shared.config import Settings
from ecommerce.shared.exceptions import NeedsAttention
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


def authenticate(context, page, settings: Settings, allow_login: bool = True) -> Page:
    """确认登录并保存会话；参数：上下文、页面、配置、是否允许扫码；返回：已登录页面。"""
    if not check_login(page):
        if not allow_login or settings.headless:
            raise NeedsAttention("登录状态失效，请使用可见浏览器执行 account login 并扫码")
        print("请在浏览器中完成扫码登录。", flush=True)
        try:
            page = login_with_qrcode(page)
        except PlaywrightTimeoutError as error:
            raise NeedsAttention(str(error)) from error
        page.wait_for_timeout(3000)
        if not check_login(page):
            raise NeedsAttention("扫码后仍无法确认登录，请检查店铺页面后重新执行")
    save_session(context, settings)
    return page


def login(settings: Settings, on_attention=None) -> str:
    """登录并保存状态；参数：配置；返回：结果说明。"""
    with browser_session(settings, on_attention=on_attention) as (context, page):
        authenticate(context, page, settings)
    return "登录成功，已保存可复用的会话"


def check(settings: Settings, on_attention=None) -> str:
    """在线检查已存会话；参数：配置；返回：结果说明。"""
    with browser_session(settings, on_attention=on_attention) as (context, page):
        authenticate(context, page, settings, allow_login=False)
    return "店铺登录状态有效"
