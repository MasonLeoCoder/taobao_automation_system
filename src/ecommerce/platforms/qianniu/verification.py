"""千牛任务级验证检测、人工处理和可替换处理器接口。"""
import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Protocol
from playwright.sync_api import Page, Error as PlaywrightError
from ecommerce.shared.exceptions import NeedsAttention

_ACTIVE = ContextVar("qianniu_verification_guard", default=None)


@dataclass(frozen=True)
class VerificationRequest:
    """处理请求：验证所在页面、业务步骤、是否首次提醒。"""
    page: Page
    step: str
    first_notice: bool


class VerificationHandler(Protocol):
    """扩展接口：阻塞到处理完成或抛错，返回后复查。

    可能在 Playwright 定位器回调中执行：仅等待人工/外部服务，
    不得调用带自动等待的 Locator 操作或从其他线程操作同步 Page。"""
    def __call__(self, request: VerificationRequest) -> None:
        ...


class ManualVerificationHandler:
    """把验证请求交给入口回调，页面自行消失不能代替用户反馈。"""
    def __init__(self, on_attention: Callable[[str], None] | None):
        self.on_attention = on_attention

    def __call__(self, request: VerificationRequest) -> None:
        title = "【需要人工验证】" if request.first_notice else "【验证尚未通过】"
        message = (f"{title}\n当前步骤：{request.step}\n"
                   "请在当前千牛浏览器中手动完成验证，验证通过后反馈“继续”。没有反馈将一直暂停。")
        if self.on_attention is None:
            raise NeedsAttention(message)
        self.on_attention(message)


def has_human_verification(page: Page) -> bool:
    """检查滑块弹窗；参数：页面；返回：任一框架中是否有可见验证。"""
    for frame in page.frames:
        try:
            if frame.locator(".J_MIDDLEWARE_FRAME_WIDGET:visible, .nc-container .nc_scale:visible").count():
                return True
            if frame.get_by_text("请拖动下方滑块完成验证", exact=False).first.is_visible():
                return True
        except PlaywrightError as error:
            if page.is_closed():
                raise NeedsAttention("验证检测期间页面已关闭") from error
            if frame.is_detached() or "Execution context was destroyed" in str(error):
                continue
            raise
    return False


def wait_for_human_verification(page, on_attention=None, step="千牛后台操作"):
    """兼容单页面调用；任务中统一使用上下文级检测。"""
    guard = _ACTIVE.get()
    if guard is not None:
        guard.checkpoint(step)
        return
    handler = ManualVerificationHandler(on_attention)
    first = True
    while has_human_verification(page):
        handler(VerificationRequest(page, step, first))
        first = False


class VerificationGuard:
    """覆盖已有/新标签页与其中的框架，统一管理处理器和操作检查。"""
    def __init__(self, context, on_attention=None, *, handler: VerificationHandler | None = None):
        self.context = context
        self.manual_handler = ManualVerificationHandler(on_attention)
        self.handler = handler if handler is not None else self.manual_handler
        self.step = "千牛后台操作"
        self._handling = False
        self._pages = {}
        self._token = None
        self.handled_count = 0
        self.failure = None

    def __enter__(self):
        self._token = _ACTIVE.set(self)
        self.context.on("page", self._attach)
        try:
            for page in self.context.pages:
                self._attach(page)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _attach(self, page):
        """在每次可操作性检查时扫描所有页面；不修改 Playwright 内部实现。"""
        if page in self._pages or page.is_closed():
            return
        body = page.locator("body")
        page.add_locator_handler(body, self._before_action, no_wait_after=True)
        self._pages[page] = body

    def _before_action(self):
        """回调抛错本身不能阻止原点击；失败时关闭任务上下文并保留原错误。"""
        try:
            self.checkpoint()
        except Exception as error:
            self.failure = error
            logging.getLogger(__name__).warning("验证无法继续，关闭任务浏览器以阻止后续动作：%s", self.step)
            self.context.close()

    def checkpoint(self, step=None):
        """暂停直到处理器返回且所有已识别验证消失；人工等待不设截止时间。"""
        if self.failure is not None:
            raise self.failure
        if self._handling:
            return
        self._handling = True
        label = step or self.step
        first = True
        try:
            while True:
                pages = [p for p in self.context.pages if not p.is_closed()]
                if not pages:
                    raise NeedsAttention("千牛浏览器已关闭，无法继续当前任务")
                blocked = next((p for p in pages if has_human_verification(p)), None)
                if blocked is None:
                    if not first:
                        logging.getLogger(__name__).info("验证已通过，恢复步骤：%s", label)
                    return
                if first:
                    logging.getLogger(__name__).warning("等待验证处理：%s", label)
                else:
                    logging.getLogger(__name__).warning("处理器已返回但验证仍存在：%s", label)
                # 扩展处理器一次返回后仍有验证则交还人工，避免空转或无限自动重试。
                processor = self.handler if first else self.manual_handler
                self.handled_count += 1
                processor(VerificationRequest(blocked, label, first))
                first = False
        finally:
            self._handling = False

    def __exit__(self, *exc):
        self.context.remove_listener("page", self._attach)
        for page, locator in self._pages.items():
            if not page.is_closed():
                try:
                    page.remove_locator_handler(locator)
                except PlaywrightError:
                    logging.getLogger(__name__).warning("验证监听清理失败，浏览器会话将关闭")
        self._pages.clear()
        if self._token is not None:
            _ACTIVE.reset(self._token)
            self._token = None


def checkpoint(step=None):
    """供长轮询使用；参数为步骤说明；返回：本任务已处理验证的次数。"""
    guard = _ACTIVE.get()
    if guard is not None:
        guard.checkpoint(step)
        return guard.handled_count
    return 0


def qianniu_operation(function):
    """所有千牛操作的步骤边界：前后检查、错误检查、不自动重放业务。"""
    @wraps(function)
    def wrapped(*args, **kwargs):
        guard = _ACTIVE.get()
        if guard is None:
            return function(*args, **kwargs)
        previous = guard.step
        guard.step = (function.__doc__ or function.__name__).split("；")[0].split("。 ")[0].splitlines()[0]
        try:
            guard.checkpoint()
            count = guard.handled_count
            try:
                result = function(*args, **kwargs)
            except (PlaywrightError, AssertionError) as error:
                guard.checkpoint()
                if guard.handled_count != count:
                    raise NeedsAttention(f"验证已处理，但步骤“{guard.step}”结果尚未确认。为避免重复操作，已停止；请核对页面后继续。") from error
                raise
            guard.checkpoint()
            return result
        finally:
            guard.step = previous
    return wrapped
