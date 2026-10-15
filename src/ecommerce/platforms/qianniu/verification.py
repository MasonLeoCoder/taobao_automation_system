"""千牛任务级验证检测、人工处理和可替换处理器接口。"""
import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Protocol
from playwright.sync_api import Page, Error as PlaywrightError, expect as playwright_expect
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
    def __init__(self, context, on_attention=None, *, handler: VerificationHandler | None = None, popup_guard=None):
        self.context = context
        self.manual_handler = ManualVerificationHandler(on_attention)
        self.handler = handler if handler is not None else self.manual_handler
        self.step = "千牛后台操作"
        self._handling = False
        self._pages = {}
        self._token = None
        self.handled_count = 0
        self.feedback_count = 0
        self.failure = None
        self.popup_guard = popup_guard
        self.operation = ""

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
        if self.popup_guard is not None:
            self.popup_guard.attach(page, lambda: self.operation, self._fail)

    def _fail(self, error):
        """阻止等待中的动作在人工入口失效后继续；参数：原因；返回：无。"""
        self.failure = error
        logging.getLogger(__name__).warning("人工处理无法继续，关闭任务浏览器以阻止后续动作：%s", self.step)
        try:
            self.context.close()
        except PlaywrightError:
            logging.getLogger(__name__).warning("浏览器已经关闭，保留原人工处理中断原因")

    def finish_interception(self):
        """派发尚未完成的拦截；参数：无；返回：人工回调完成后返回，不重放业务。"""
        # 原操作可能在截图等嵌套调用期间先返回超时，必须等拦截回调完成再结束任务。
        while self._handling or (self.popup_guard is not None and self.popup_guard.native_handling):
            if self.failure is not None:
                raise self.failure
            pages = [page for page in self.context.pages if not page.is_closed()]
            if not pages:
                raise NeedsAttention("人工处理期间浏览器已关闭，请核对当前操作结果")
            pages[-1].wait_for_timeout(25)

    def _before_action(self):
        """回调抛错本身不能阻止原点击；失败时关闭任务上下文并保留原错误。"""
        try:
            self.checkpoint()
        except Exception as error:
            self._fail(error)

    @property
    def interruption_count(self):
        """汇总人工及公告处理次数；参数：无；返回：步骤中断计数，用于禁止不确定重放。"""
        return self.handled_count + (self.popup_guard.handled_count if self.popup_guard is not None else 0)

    @property
    def resume_count(self):
        """统计有效处理反馈；参数：无；返回：反馈次数，用于避免原动作超时后重复提问。"""
        return self.feedback_count + (self.popup_guard.feedback_count if self.popup_guard is not None else 0)

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
                    if self.popup_guard is not None:
                        self.popup_guard.checkpoint(self.operation, label)
                    return
                if first:
                    logging.getLogger(__name__).warning("等待验证处理：%s", label)
                else:
                    logging.getLogger(__name__).warning("处理器已返回但验证仍存在：%s", label)
                # 扩展处理器一次返回后仍有验证则交还人工，避免空转或无限自动重试。
                processor = self.handler if first else self.manual_handler
                self.handled_count += 1
                processor(VerificationRequest(blocked, label, first))
                self.feedback_count += 1
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
        if self.popup_guard is not None:
            self.popup_guard.close()
        if self._token is not None:
            _ACTIVE.reset(self._token)
            self._token = None


def checkpoint(step=None):
    """供长轮询使用；参数为步骤说明；返回：本任务已处理验证的次数。"""
    guard = _ACTIVE.get()
    if guard is not None:
        guard.checkpoint(step)
        return guard.interruption_count
    return 0


def page_step(action, *, description=None, completed=None):
    """恢复一个可重复核对的子步骤；参数：只读/幂等动作、说明、可选完成判定；返回：动作结果。

    只包裹一个等待或有完成判定的局部动作，禁止包裹整个上传、创建或提交函数。
    每次失败后必须取得反馈；已完成的局部动作不重做，人工等待不限制次数。
    """
    guard = _ACTIVE.get()
    if guard is None:
        return action()
    label = description or guard.step
    def finished():
        if completed is None:
            return False
        try:
            return bool(completed())
        except PlaywrightError:
            # 控件可能刚被移除；由后续同一子步骤和回读核对处理，不猜测成功。
            return False
    while True:
        guard.checkpoint(label)
        count = guard.resume_count
        try:
            return action()
        except (PlaywrightError, AssertionError) as error:
            guard.finish_interception()
            guard.checkpoint(label)
            if finished():
                logging.getLogger(__name__).info("子步骤已生效，核对后继续：%s", label)
                return
            if count == guard.resume_count:
                if guard.popup_guard is None:
                    raise
                guard.popup_guard.review_error(guard.operation, label, error)
            guard.checkpoint(label)
            if finished():
                logging.getLogger(__name__).info("人工处理后子步骤已完成，继续下一步：%s", label)
                return
            logging.getLogger(__name__).info("人工处理已完成，恢复当前子步骤：%s", label)


class _RecoverableAssertions:
    """只包装公开断言；在原调用位置重新核对，不重跑业务函数。"""
    def __init__(self, assertions, message=None):
        self.assertions = assertions
        self.message = message

    def __getattr__(self, name):
        attribute = getattr(self.assertions, name)
        if not callable(attribute):
            return attribute
        def checked(*args, **kwargs):
            return page_step(lambda: attribute(*args, **kwargs), description=self.message)
        return checked


def expect(actual, message=None):
    """创建可恢复的只读断言；参数：定位器及说明；返回：在同一位置等待人工反馈的断言。"""
    return _RecoverableAssertions(playwright_expect(actual, message), message)


@contextmanager
def intercept_file_dialogs(page):
    """在上传操作全程接管系统选文件请求；参数：页面；返回：退出时释放监听的作用域。

    临时的获取请求监听结束后，组件仍可能发出额外请求。持续监听避免其弹出系统窗口；
    此处不提供文件、不重放上传，只有调用方明确捕获的请求可以设置文件。
    """
    def intercept(chooser):
        """接管选文件请求；参数：请求；返回：无，不提供文件或重放上传。"""
        logging.getLogger(__name__).debug("上传期间已接管文件选择请求，不打开系统文件窗口")
    page.on("filechooser", intercept)
    try:
        yield
    finally:
        page.remove_listener("filechooser", intercept)


def choose_file_dialog(page, trigger, timeout=30000):
    """获取一次文件选择请求；参数：页面、入口定位器、等待时限；返回：文件选择器，不上传文件。

    上传调用方须用intercept_file_dialogs覆盖完整操作；本函数仅临时捕获本次请求。
    在人工等待导致点击超时后复用已捕获的请求，避免再次点击或丢失文件请求。
    """
    captured = []
    def capture(chooser):
        captured.append(chooser)
    def obtain():
        if not captured:
            trigger.click(timeout=timeout)
        if not captured:
            captured.append(page.wait_for_event("filechooser", timeout=timeout))
        return captured[0]
    page.on("filechooser", capture)
    try:
        return page_step(obtain, description="打开本地文件选择入口")
    finally:
        page.remove_listener("filechooser", capture)


def qianniu_operation(function):
    """所有千牛操作的步骤边界：前后检查、错误检查、不自动重放业务。"""
    @wraps(function)
    def wrapped(*args, **kwargs):
        guard = _ACTIVE.get()
        if guard is None:
            return function(*args, **kwargs)
        previous = guard.step
        previous_operation = guard.operation
        guard.operation = f"{getattr(function, '__module__', '').rsplit('.', 1)[-1]}.{getattr(function, '__name__', '')}"
        guard.step = (function.__doc__ or function.__name__).split("；")[0].split("。 ")[0].splitlines()[0]
        try:
            guard.checkpoint()
            count = guard.interruption_count
            try:
                result = function(*args, **kwargs)
            except (PlaywrightError, AssertionError) as error:
                guard.finish_interception()
                guard.checkpoint()
                if guard.interruption_count != count:
                    raise NeedsAttention(f"验证或弹窗已处理，但步骤“{guard.step}”结果尚未确认。为避免重复操作，已停止；请核对页面后继续。") from error
                # 已离开原调用栈的未知代码错误不能伪装成局部恢复；可恢复等待须使用page_step。
                raise
            guard.finish_interception()
            guard.checkpoint()
            return result
        finally:
            guard.step = previous
            guard.operation = previous_operation
    return wrapped
