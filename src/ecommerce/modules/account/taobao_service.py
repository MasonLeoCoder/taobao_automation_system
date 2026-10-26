"""淘宝买家账号流程；不导入千牛登录判断或千牛页面守卫。"""
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ecommerce.infrastructure.browser.session import save_session
from ecommerce.platforms.taobao.session import browser_session
from ecommerce.platforms.taobao.login import check_login, is_authenticated, login_manually, wait_until_browser_closed
from ecommerce.platforms.taobao.verification import active_guard, checkpoint
from ecommerce.shared.exceptions import NeedsAttention


def authenticate(context, page, settings, allow_login=True, *, on_login_required=None):
    """确认淘宝在线会话，只有复核通过后才保存状态。"""
    try:
        authenticated = check_login(page, settings.timeout_ms)
        if not authenticated:
            if not allow_login or settings.headless:
                raise NeedsAttention("淘宝会话失效或未能确认，请执行 account login --platform taobao 手动登录")
            page = login_manually(page, settings.timeout_ms,on_login_required=on_login_required) if on_login_required else login_manually(page,settings.timeout_ms)
            if not check_login(page, settings.timeout_ms):
                raise NeedsAttention("登录后未能确认淘宝首页账号状态，未保存登录状态")
    except PlaywrightTimeoutError as error:
        raise NeedsAttention("淘宝页面访问超时，未确认或保存登录状态，请检查网络后重试") from error
    guard = active_guard()
    if guard is not None:
        guard.login_mode = False
        guard.resume_check = lambda: is_authenticated(page)
    checkpoint("保存淘宝登录状态")
    save_session(context, settings)
    return page


def login(settings, on_attention=None, *, wait=True):
    """登录保存后默认保留窗口，直到用户手动关闭。"""
    with browser_session(settings, on_attention=on_attention) as (context, page):
        authenticate(context, page, settings)
        if wait and not settings.headless:
            wait_until_browser_closed(context)
    return "淘宝登录成功，已保存独立会话（与千牛分开）"


def check(settings, on_attention=None):
    """检查淘宝会话，不自动跳转到登录流程。"""
    with browser_session(settings, on_attention=on_attention) as (context, page):
        authenticate(context, page, settings, allow_login=False)
    return "淘宝登录状态有效（与千牛分开）"
