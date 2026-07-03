"""淘宝卖家中心登录流程。"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    ElementHandle,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from utils.logger import get_logger

LOGIN_URL = "https://loginmyseller.taobao.com/"
HOME_URL = "https://myseller.taobao.com/home.htm/QnworkbenchHome"
STATE_PATH = Path("./state.json")
USER_DATA_DIR = Path("./storage/taobao_browser_profile")
SESSION_MARKER_PATH = Path("./storage/taobao_browser_profile/.login_ok")
MAX_RETRY = 3
PAGE_TIMEOUT_MS = 60_000
SHORT_TIMEOUT_MS = 5_000
SCAN_TIMEOUT_MS = 180_000

LOGIN_ROOT_SELECTOR = "#login"
QRCODE_SELECTOR = "#login .icon-qrcode"
LOGIN_FORM_SELECTORS = (
    "#login",
    "#login .icon-qrcode",
    "iframe[src*='login']",
)
HOME_HINT_SELECTORS = (
    "a[href*='logout']",
    "a[href*='login/logout']",
    ".site-nav-user",
    ".seller-nav",
    "[class*='seller']",
)


def launch_browser() -> tuple[Playwright, BrowserContext]:
    """启动浏览器。

    参数：
        无。

    返回：
        tuple[Playwright, BrowserContext]：Playwright 实例和持久化浏览器上下文。
    """
    playwright = sync_playwright().start()
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        headless=False,
    )
    context.set_default_timeout(PAGE_TIMEOUT_MS)
    context.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
    return playwright, context


def load_session(context: BrowserContext) -> bool:
    """加载本地登录会话。

    参数：
        context (BrowserContext)：需要注入登录态的浏览器上下文。

    返回：
        bool：会话文件存在且加载成功时返回 True，否则返回 False。
    """
    logger = get_logger(__name__)
    if not _has_saved_session():
        return False

    state = _read_storage_state()
    if state is None:
        logger.info("Loaded session from persistent browser profile: %s", USER_DATA_DIR)
        return True

    cookies = state.get("cookies", [])
    if cookies:
        context.add_cookies(cookies)

    origins = state.get("origins", [])
    if origins:
        storage_payload = json.dumps(
            {
                origin.get("origin"): {
                    item.get("name"): item.get("value")
                    for item in origin.get("localStorage", [])
                    if item.get("name") is not None
                }
                for origin in origins
                if origin.get("origin")
            },
            ensure_ascii=True,
        )
        context.add_init_script(
            f"""
            (() => {{
                const storageByOrigin = {storage_payload};
                const values = storageByOrigin[window.location.origin];
                if (!values) return;
                for (const [key, value] of Object.entries(values)) {{
                    window.localStorage.setItem(key, value);
                }}
            }})();
            """
        )

    logger.info(
        "Loaded session from profile %s and state file %s; cookies=%s, origins=%s.",
        USER_DATA_DIR,
        STATE_PATH,
        len(cookies),
        len(origins),
    )
    return True


def check_login(page: Page) -> bool:
    """判断当前会话是否已登录。

    参数：
        page (Page)：用于访问淘宝卖家中心登录入口的页面对象。

    返回：
        bool：已登录返回 True，未登录或检查失败返回 False。
    """
    logger = get_logger(__name__)

    for attempt in range(1, MAX_RETRY + 1):
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            _wait_for_page_ready(page)
            _human_delay(page)

            if _has_visible_selector(page, HOME_HINT_SELECTORS, timeout=SHORT_TIMEOUT_MS):
                logger.info("Login is valid; seller-center page indicator found.")
                return True

            if _has_visible_selector(page, LOGIN_FORM_SELECTORS, timeout=SHORT_TIMEOUT_MS):
                logger.info("Login page is visible; session is not authenticated.")
                return False

            if "login" not in page.url.lower():
                logger.info("Login is valid; seller homepage URL is %s", page.url)
                return True

            return False
        except PlaywrightTimeoutError as error:
            logger.warning("Login check attempt %s/%s timed out: %s", attempt, MAX_RETRY, error)
            if attempt == MAX_RETRY:
                return False
            page.reload(wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(1_000 * attempt)

    return False


# 点击扫码登陆
def login_with_qrcode(page: Page) -> bool:
    """执行二维码扫码登录。

    参数：
        page (Page)：用于展示登录页并等待人工扫码的页面对象。

    返回：
        bool：扫码登录成功返回 True，失败时抛出 RuntimeError。
    """
    logger = get_logger(__name__)

    for attempt in range(1, MAX_RETRY + 1):
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            _wait_for_page_ready(page)
            _human_delay(page)

            qrcode_button = _wait_for_selector_any_frame(
                page,
                QRCODE_SELECTOR,
                state="visible",
                timeout=PAGE_TIMEOUT_MS,
            )
            _human_delay(page)
            qrcode_button.click()
            _human_delay(page)

            logger.info("Waiting for QR-code scan to complete. Attempt %s/%s.", attempt, MAX_RETRY)
            _wait_for_login_success(page)
            logger.info("QR-code login succeeded on current page: %s", page.url)
            return True
        except PlaywrightTimeoutError as error:
            logger.warning("QR-code login attempt %s/%s timed out: %s", attempt, MAX_RETRY, error)
            if attempt == MAX_RETRY:
                raise RuntimeError("QR-code login timed out after 3 attempts.") from error
            page.reload(wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(1_000 * attempt)

    raise RuntimeError("QR-code login failed after 3 attempts.")


def save_session(context: BrowserContext) -> None:
    """保存登录会话。

    参数：
        context (BrowserContext)：包含当前登录态的浏览器上下文。

    返回：
        None：该函数只负责将 storage_state 写入 ./state.json。
    """
    context.storage_state(path=str(STATE_PATH))
    SESSION_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_MARKER_PATH.write_text(str(int(time.time())), encoding="utf-8")
    get_logger(__name__).info("Saved session to %s", STATE_PATH)


# 登陆
def ensure_login() -> bool:
    """编排淘宝卖家中心登录流程。

    参数：
        无。

    返回：
        bool：登录态可用或扫码登录成功返回 True，否则返回 False。
    """
    logger = get_logger(__name__)
    playwright: Playwright | None = None
    context: BrowserContext | None = None

    try:
        playwright, context = launch_browser()
        session_loaded = False

        if _has_saved_session():
            logger.info("Session exists; loading before login check.")
            session_loaded = load_session(context)

        page = context.new_page()

        if session_loaded:
            if check_login(page):
                save_session(context)
                return True

            logger.info("Stored session is invalid; falling back to QR-code login.")
        else:
            logger.info("Session does not exist; starting QR-code login.")

        login_with_qrcode(page)
        _wait_for_auth_state_stable(page)
        save_session(context)
        return True
    except Exception:
        logger.exception("Taobao seller-center login failed.")
        return False
    finally:
        if context is not None:
            context.close()
        if playwright is not None:
            playwright.stop()


def _wait_for_page_ready(page: Page) -> None:
    """等待页面进入可操作状态。

    参数：
        page (Page)：需要等待加载完成的页面对象。

    返回：
        None：该函数只负责等待，不返回业务数据。
    """
    page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=SHORT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(800)


def _read_storage_state() -> dict | None:
    """读取并校验本地 storage_state 文件。

    参数：
        无。

    返回：
        dict | None：文件存在且 JSON 合法时返回状态字典，否则返回 None。
    """
    logger = get_logger(__name__)

    if not STATE_PATH.exists():
        logger.info("No session file found: %s", STATE_PATH)
        return None

    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Session file is not valid JSON: %s", STATE_PATH)
        return None

    if not isinstance(state, dict):
        logger.warning("Session file root is not a JSON object: %s", STATE_PATH)
        return None

    return state


def _has_saved_session() -> bool:
    """判断本地是否存在可复用的登录会话。

    参数：
        无。

    返回：
        bool：扫码成功后存在持久化浏览器标记时返回 True，否则返回 False。
    """
    return SESSION_MARKER_PATH.exists()


def _wait_for_auth_state_stable(page: Page) -> None:
    """等待登录后的 cookie 和本地存储写入稳定。

    参数：
        page (Page)：扫码登录成功后的当前页面对象。

    返回：
        None：该函数只负责等待状态稳定，不返回业务数据。
    """
    _wait_for_page_ready(page)
    page.wait_for_timeout(3_000)


def _has_visible_selector(page: Page, selectors: tuple[str, ...], timeout: int) -> bool:
    """检查多个选择器中是否存在可见元素。

    参数：
        page (Page)：执行元素查询的页面对象。
        selectors (tuple[str, ...])：需要依次检查的选择器列表。
        timeout (int)：每个选择器的等待超时时间，单位为毫秒。

    返回：
        bool：任意选择器命中可见元素返回 True，否则返回 False。
    """
    for selector in selectors:
        try:
            _wait_for_selector_any_frame(page, selector, state="visible", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            continue
    return False


def _wait_for_selector_any_frame(
    page: Page,
    selector: str,
    state: str,
    timeout: int,
) -> ElementHandle:
    """在主页面和所有 iframe 中等待元素。

    参数：
        page (Page)：需要查找元素的页面对象。
        selector (str)：需要等待的 CSS 选择器。
        state (str)：元素等待状态，例如 attached 或 visible。
        timeout (int)：总等待超时时间，单位为毫秒。

    返回：
        ElementHandle：命中的元素句柄，可继续执行点击等操作。
    """
    deadline = time.monotonic() + timeout / 1_000
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
        slice_timeout = min(500, remaining_ms)

        try:
            element = page.wait_for_selector(selector, state=state, timeout=slice_timeout)
            if element is not None:
                return element
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                element = frame.wait_for_selector(selector, state=state, timeout=slice_timeout)
                if element is not None:
                    return element
            except (PlaywrightTimeoutError, PlaywrightError) as error:
                last_error = error

        page.wait_for_timeout(200)

    raise PlaywrightTimeoutError(
        f"Timeout {timeout}ms waiting for selector {selector!r} in page or iframe."
    ) from last_error


def _wait_for_login_success(page: Page) -> None:
    """等待扫码后跳转到已登录页面。

    参数：
        page (Page)：完成扫码后等待跳转的页面对象。

    返回：
        None：登录成功后正常返回，超时则抛出 PlaywrightTimeoutError。
    """
    deadline = time.monotonic() + SCAN_TIMEOUT_MS / 1_000

    while time.monotonic() < deadline:
        _wait_for_page_ready(page)

        if _has_visible_selector(page, HOME_HINT_SELECTORS, timeout=1_000):
            return

        if "login" not in page.url.lower():
            page.wait_for_timeout(1_500)
            return

        page.wait_for_timeout(1_000)

    raise PlaywrightTimeoutError("Timeout waiting for QR-code login success.")


def _human_delay(page: Page) -> None:
    """加入随机等待，降低页面未稳定时操作的概率。

    参数：
        page (Page)：执行等待的页面对象。

    返回：
        None：该函数只负责等待，不返回业务数据。
    """
    delay_ms = random.randint(400, 1_200)
    page.wait_for_timeout(delay_ms)
