"""千牛登录页面操作，业务流程由账号模块编排。"""
from .verification import qianniu_operation, checkpoint, page_step
import time
from logging import getLogger as get_logger
from urllib.parse import urlsplit
from playwright.sync_api import Locator, Page, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

LOGIN_URL = "https://loginmyseller.taobao.com/"
HOME_URL = "https://myseller.taobao.com/home.htm/QnworkbenchHome"
MAX_RETRY = 3
PAGE_TIMEOUT_MS = 60_000
SHORT_TIMEOUT_MS = 5_000
SCAN_TIMEOUT_MS = 180_000
QRCODE_SELECTOR = "#login .icon-qrcode"
LOGIN_FORM_SELECTORS = ("#login", "#login .icon-qrcode")
HOME_HINT_SELECTORS = (
    "a[href*='logout']",
    ".seller-nav",
    "a[href*='/home.htm/_firstmenuid_/2274']",
    "a[href*='upload.taobao.com/auction/sell.jhtml']",
)
HOME_NAV_SELECTORS = ('text="首页"',)
TRADE_NAV_SELECTORS = ('text="交易"',)


def _visible(page: Page, selectors: tuple[str, ...]) -> bool:
    """即时检查主页面及框架；参数：页面、选择器；返回：是否存在可见元素。"""
    for frame in page.frames:
        for selector in selectors:
            try:
                locator = frame.locator(selector)
                for index in range(locator.count()):
                    if locator.nth(index).is_visible():
                        return True
            except PlaywrightError:
                # 页面跳转或框架销毁时留待下一轮检查。
                continue
    return False


def _is_authenticated(page: Page) -> bool:
    """结合工作台地址和可见导航判定；参数：页面；返回：是否已登录。"""
    if page.is_closed():
        return False
    url = urlsplit(page.url)
    if url.scheme != "https" or url.hostname != "myseller.taobao.com":
        return False
    if url.path != "/home.htm" and not url.path.startswith("/home.htm/"):
        return False
    if _visible(page, LOGIN_FORM_SELECTORS):
        return False
    return _visible(page, HOME_HINT_SELECTORS) or (
        _visible(page, HOME_NAV_SELECTORS) and _visible(page, TRADE_NAV_SELECTORS)
    )


def _safe_location(page: Page) -> str:
    """生成不包含查询参数的诊断位置；参数：页面；返回：主机和路径。"""
    url = urlsplit(page.url)
    return f"{url.scheme}://{url.netloc}{url.path}"


@qianniu_operation
def check_login(page: Page) -> bool:
    """访问工作台确认会话；参数：页面；返回：是否已登录。"""
    logger = get_logger(__name__)
    for attempt in range(1, MAX_RETRY + 1):
        try:
            response = page.goto(HOME_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            if response is not None and response.status >= 400:
                logger.warning("工作台返回 HTTP %s，未确认登录", response.status)
                return False
            deadline = time.monotonic() + PAGE_TIMEOUT_MS / 1000
            while time.monotonic() < deadline:
                before_check = time.monotonic()
                checkpoint("检查登录状态")
                deadline += time.monotonic() - before_check
                if _is_authenticated(page):
                    logger.info("已确认千牛工作台登录：%s", _safe_location(page))
                    return True
                if _visible(page, LOGIN_FORM_SELECTORS):
                    logger.info("检测到登录表单，需要扫码登录")
                    return False
                page.wait_for_timeout(300)
            logger.warning("工作台登录状态未能识别：%s", _safe_location(page))
            return False
        except PlaywrightTimeoutError:
            if checkpoint("访问工作台超时，检查验证状态"):
                raise
            logger.warning("工作台访问超时，第 %s/%s 次", attempt, MAX_RETRY)
    return False


@qianniu_operation
def login_with_qrcode(page: Page, *, on_wait=None) -> Page:
    """显示扫码页；参数：页面、可选人工等待回调；返回：平台已确认登录的页面。"""
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    # 登录入口也可能在加载后跳转，等待期间同时检查扫码入口和工作台。
    entry = page_step(lambda: _wait_for_scan_entry(page),
                      completed=lambda: _authenticated_tab(page) is not None)
    if entry is None:
        return _wait_for_login_success(page)
    authenticated, qrcode_button = entry
    if authenticated is not None:
        return authenticated
    checkpoint("切换扫码登录")
    if (authenticated := _authenticated_tab(page)) is not None:
        return authenticated
    try:
        page_step(qrcode_button.click, completed=lambda: _authenticated_tab(page) is not None)
    except PlaywrightError:
        if (authenticated := _authenticated_tab(page)) is not None:
            return authenticated
        raise
    get_logger(__name__).info("请扫码登录，等待识别千牛工作台；超时后不会自动刷新并重复扫码")
    if on_wait is None:
        authenticated = _wait_for_login_success(page)
    else:
        retry = False
        while True:
            # 网页明确反馈后才查询是否已登录；反馈过早时保留原扫码页重新提示。
            on_wait(page, retry=retry)
            try:
                authenticated = _wait_for_login_success(page, wait_ms=3500)
                break
            except PlaywrightTimeoutError:
                if not any(not candidate.is_closed() for candidate in page.context.pages):
                    raise
                retry = True
    get_logger(__name__).info("扫码登录成功：%s", _safe_location(authenticated))
    return authenticated


def _authenticated_tab(page: Page) -> Page | None:
    return next((tab for tab in page.context.pages if _is_authenticated(tab)), None)


def _wait_for_scan_entry(page: Page) -> tuple[Page | None, Locator | None]:
    """等待扫码按钮或已登录的工作台，处理当前页跳转和新标签页。"""
    deadline = time.monotonic() + PAGE_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        before_check = time.monotonic()
        checkpoint("显示扫码页")
        deadline += time.monotonic() - before_check
        if (authenticated := _authenticated_tab(page)) is not None:
            return authenticated, None
        tabs = [tab for tab in page.context.pages if not tab.is_closed()]
        if not tabs:
            raise PlaywrightTimeoutError("登录浏览器已关闭，请重新登录")
        if not page.is_closed():
            for frame in page.frames:
                try:
                    for button in frame.locator(QRCODE_SELECTOR).all():
                        if button.is_visible():
                            return None, button
                except PlaywrightError:
                    # 页面可能已跳转，下一轮重新检查工作台和扫码按钮。
                    continue
        try:
            tabs[-1].wait_for_timeout(250)
        except PlaywrightError:
            if not page.context.pages:
                raise
    raise PlaywrightTimeoutError("未找到千牛扫码入口，也未确认已进入千牛工作台")


def _wait_for_login_success(page: Page, *, wait_ms=None) -> Page:
    """轮询当前上下文；参数：原扫码页面、复核时限；返回：工作台页面，超时抛出异常。"""
    context = page.context
    deadline = time.monotonic() + (SCAN_TIMEOUT_MS if wait_ms is None else wait_ms) / 1000
    while time.monotonic() < deadline:
        before_check = time.monotonic()
        checkpoint("等待扫码登录")
        deadline += time.monotonic() - before_check
        candidates = [candidate for candidate in context.pages if not candidate.is_closed()]
        if (authenticated := _authenticated_tab(page)) is not None:
            return authenticated
        if not candidates:
            raise PlaywrightTimeoutError("登录浏览器已关闭，请重新执行 account login")
        # 用 Playwright 的等待派发导航事件，不等待持续联网页面进入 networkidle。
        try:
            candidates[-1].wait_for_timeout(300)
        except PlaywrightError:
            if context.pages:
                continue
            raise
    locations = ", ".join(_safe_location(candidate) for candidate in context.pages if not candidate.is_closed())
    raise PlaywrightTimeoutError(
        f"扫码等待超时，未识别到千牛工作台导航。当前页面：{locations or '无'}。"
        "若已登录，请提供当前网址及可见导航；程序未自动重新扫码。"
    )
