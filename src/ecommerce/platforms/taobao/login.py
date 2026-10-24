"""淘宝买家端登录，独立于千牛的入口及登录判断。"""
from ecommerce.infrastructure.tasks.events import notify as print
import time
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError

from ecommerce.shared.exceptions import NeedsAttention, TaskStopped
from ecommerce.platforms.taobao.verification import active_guard, checkpoint, wait

HOME_URL = "https://www.taobao.com/"
LOGIN_URL = "https://login.taobao.com/member/login.jhtml?" + urlencode({"redirectURL": HOME_URL})
CHECK_TIMEOUT_MS = 10_000
NICK_SELECTORS = (".site-nav-login-info-nick", ".J_UserMemberNick")
ANONYMOUS_SELECTOR = '.site-nav-login a[href*="login.taobao.com"]'


def wait_until_browser_closed(context) -> None:
    """登录成功后保持事件循环，直到用户关闭最后一个淘宝窗口。"""
    print("淘宝登录已确认，独立会话已保存。浏览器将保持打开，请手动关闭淘宝浏览器以结束本次任务。", flush=True)
    while True:
        pages = [page for page in context.pages if not page.is_closed()]
        if not pages:
            return
        try:
            checkpoint("淘宝登录成功，保留窗口")
            wait(pages[-1], 300)
        except TaskStopped:
            guard = active_guard()
            if not context.pages and (guard is None or not guard.awaiting_manual):
                return
            raise
        except PlaywrightError:
            # 当前标签页关闭后重新检查其余标签页，最后一个窗口关闭才结束。
            if pages[-1].is_closed() or not context.pages:
                continue
            raise


def _visible_text(page: Page, selector: str) -> bool:
    """只接受可见且非空的元素，避免未登录页面的隐藏模板误判。"""
    for locator in page.locator(selector).all():
        if locator.is_visible() and locator.inner_text(timeout=1000).strip():
            return True
    return False


def is_authenticated(page: Page) -> bool:
    """以淘宝首页中的实际账号昵称确认登录，不依赖网址或 Cookie 的存在。"""
    try:
        if page.is_closed():
            return False
        url = urlsplit(page.url)
        if url.scheme != "https" or url.hostname not in {"www.taobao.com", "taobao.com"}:
            return False
        if url.path not in {"", "/", "/index.php"}:
            return False
        if _visible_text(page, ANONYMOUS_SELECTOR):
            return False
        return any(_visible_text(page, selector) for selector in NICK_SELECTORS)
    except PlaywrightError:
        # 页面正在导航或关闭，下一轮重新检查。
        return False


def check_login(page: Page, timeout_ms: int = CHECK_TIMEOUT_MS) -> bool:
    """在线加载淘宝首页并检查登录；未知页面也不视为登录成功。"""
    guard = active_guard()
    action = lambda: page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    completed = lambda: urlsplit(page.url).hostname in {"www.taobao.com", "taobao.com"} and page.locator("body").is_visible()
    response = guard.perform("检查淘宝登录状态", action, completed=completed) if guard else action()
    if response is not None and response.status >= 400 and not is_authenticated(page):
        raise NeedsAttention(f"淘宝首页返回 HTTP {response.status}，未能检查登录状态")
    deadline = time.monotonic() + min(timeout_ms, CHECK_TIMEOUT_MS) / 1000
    while time.monotonic() < deadline:
        deadline += checkpoint("识别淘宝首页登录状态")
        if is_authenticated(page):
            return True
        deadline += wait(page, 300)
    return False


def login_manually(page: Page, timeout_ms: int) -> Page:
    """打开淘宝登录页，由用户完成扫码、短信或密码及验证，不自动刷新。"""
    try:
        guard = active_guard()
        action = lambda: page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        completed = lambda: urlsplit(page.url).hostname in {"login.taobao.com", "www.taobao.com", "taobao.com"} and page.locator("body").is_visible()
        guard.perform("打开淘宝买家登录页", action, completed=completed) if guard else action()
    except PlaywrightTimeoutError:
        # 保留已打开的页面，允许用户在慢网络下继续操作。
        print("淘宝登录页加载较慢，浏览器保持打开，请检查页面。", flush=True)
    print("请在淘宝浏览器中手动完成登录和页面验证。登录后将检查淘宝首页并保存独立会话。\n"
          "等待不设超时，不会自动刷新二维码；关闭该浏览器或按 Ctrl+C 可取消。", flush=True)
    context = page.context
    while True:
        checkpoint("等待人工登录淘宝")
        candidates = [candidate for candidate in context.pages if not candidate.is_closed()]
        if not candidates:
            raise NeedsAttention("淘宝登录浏览器已关闭，尚未确认登录，请重新执行 account login --platform taobao")
        for candidate in candidates:
            if is_authenticated(candidate):
                return candidate
        try:
            wait(candidates[-1], 300)
        except PlaywrightError:
            if not context.pages:
                raise NeedsAttention("淘宝登录浏览器已关闭，未保存登录状态") from None
