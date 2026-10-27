"""淘宝独立会话，自动安装任务级验证、弹窗和限流守卫。"""
from contextlib import contextmanager

from ecommerce.infrastructure.browser.session import browser_session as base_browser_session, restore_missing_session_cookies
from ecommerce.platforms.taobao.verification import TaobaoGuard
from ecommerce.shared.exceptions import NeedsAttention


@contextmanager
def browser_session(settings, on_attention=None):
    # 已有资料优先；只补回正常关闭后丢失的临时 Cookie，避免旧导出覆盖最新状态。
    initialized = (settings.profile_path / "Default").is_dir()
    options = {"restore_state": False} if initialized else {}
    factory = base_browser_session
    if settings.taobao_browser_mode == "native_cdp":
        from ecommerce.infrastructure.browser.native_chrome import browser_session as factory
    elif settings.taobao_browser_channel is not None:
        options["channel"] = settings.taobao_browser_channel
    with factory(settings, **options) as (context, page):
        if initialized:
            restore_missing_session_cookies(context, settings)
        with TaobaoGuard(context, settings.log_dir, on_attention, timing=settings.taobao_timing) as guard:
            try:
                yield context, page
            except NeedsAttention as error:
                if not settings.headless:
                    guard.hold_without_input(reason=str(error))
                raise
