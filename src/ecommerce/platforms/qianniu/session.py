"""千牛专用会话：任何业务都必须经过任务级验证守卫。"""
from contextlib import contextmanager
from ecommerce.infrastructure.browser.session import browser_session as base_browser_session
from .verification import VerificationGuard
from .popups import PopupGuard


@contextmanager
def browser_session(settings, on_attention=None, *, verification_handler=None):
    """创建带全局检查的会话；可注入人工回调或符合协议的验证处理器。"""
    with base_browser_session(settings) as (context, page):
        with VerificationGuard(context, on_attention, handler=verification_handler,
                               popup_guard=PopupGuard(context, on_attention, settings.log_dir)):
            yield context, page
