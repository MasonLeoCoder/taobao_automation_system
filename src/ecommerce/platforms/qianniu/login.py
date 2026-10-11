"""千牛登录页面操作，业务流程由账号模块编排。"""
import time
from logging import getLogger as get_logger
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from ecommerce.infrastructure.browser.actions import wait_for_page_ready, human_delay, wait_for_selector_any_frame, has_visible_selector

LOGIN_URL = "https://loginmyseller.taobao.com/"
HOME_URL = "https://myseller.taobao.com/home.htm/QnworkbenchHome"
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
)


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
            wait_for_page_ready(page)
            human_delay(page)

            if has_visible_selector(page, HOME_HINT_SELECTORS, timeout=SHORT_TIMEOUT_MS):
                logger.info("Login is valid; seller-center page indicator found.")
                return True

            if has_visible_selector(page, LOGIN_FORM_SELECTORS, timeout=SHORT_TIMEOUT_MS):
                logger.info("Login page is visible; session is not authenticated.")
                return False

            return False
        except PlaywrightTimeoutError as error:
            logger.warning("Login check attempt %s/%s timed out: %s", attempt, MAX_RETRY, error)
            if attempt == MAX_RETRY:
                return False
            page.reload(wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(1_000 * attempt)

    return False

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
            wait_for_page_ready(page)
            human_delay(page)

            qrcode_button = wait_for_selector_any_frame(
                page,
                QRCODE_SELECTOR,
                state="visible",
                timeout=PAGE_TIMEOUT_MS,
            )
            human_delay(page)
            qrcode_button.click()
            human_delay(page)

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

def _wait_for_login_success(page: Page) -> None:
    """等待扫码后跳转到已登录页面。

    参数：
        page (Page)：完成扫码后等待跳转的页面对象。

    返回：
        None：登录成功后正常返回，超时则抛出 PlaywrightTimeoutError。
    """
    deadline = time.monotonic() + SCAN_TIMEOUT_MS / 1_000

    while time.monotonic() < deadline:
        wait_for_page_ready(page)

        if has_visible_selector(page, HOME_HINT_SELECTORS, timeout=1_000):
            return

        page.wait_for_timeout(1_000)

    raise PlaywrightTimeoutError("Timeout waiting for QR-code login success.")
