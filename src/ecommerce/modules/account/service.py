"""账号业务流程，复用当前任务的浏览器上下文。"""
from ecommerce.infrastructure.browser.session import browser_session, save_session
from ecommerce.platforms.qianniu.login import check_login, login_with_qrcode
from ecommerce.shared.config import Settings
from ecommerce.shared.exceptions import NeedsAttention


def authenticate(context, page, settings: Settings, allow_login: bool = True) -> None:
    """确认登录并保存会话；参数：上下文、页面、配置、是否允许扫码；返回：无。"""
    if not check_login(page):
        if not allow_login or settings.headless:
            raise NeedsAttention("登录状态失效，请使用可见浏览器执行 account login 并扫码")
        print("请在浏览器中完成扫码登录。", flush=True)
        login_with_qrcode(page)
        page.wait_for_timeout(3000)
        if not check_login(page):
            raise NeedsAttention("扫码后仍无法确认登录，请检查店铺页面后重新执行")
    save_session(context, settings)


def login(settings: Settings) -> str:
    """登录并保存状态；参数：配置；返回：结果说明。"""
    with browser_session(settings) as (context, page):
        authenticate(context, page, settings)
    return "登录成功，已保存可复用的会话"


def check(settings: Settings) -> str:
    """在线检查已存会话；参数：配置；返回：结果说明。"""
    with browser_session(settings) as (context, page):
        authenticate(context, page, settings, allow_login=False)
    return "店铺登录状态有效"
