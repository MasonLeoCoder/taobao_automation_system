"""千牛商品入口操作，迁移现有选择器。"""
from logging import getLogger as get_logger
from ecommerce.infrastructure.browser.actions import wait_for_page_ready, click_and_get_next_page
from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

PRODUCT_NAV_SELECTORS = (
    "a[data-index='3'][href*='/home.htm/_firstmenuid_/2274']",
    "a[role='button'][href*='_firstmenuid_/2274']",
    "a:has-text('商品')",
)
PUBLISH_PRODUCT_SELECTORS = (
    "a[data-index='2276'][href*='upload.taobao.com/auction/sell.jhtml']",
    "a[href*='upload.taobao.com/auction/sell.jhtml']",
    "li[role='menuitem']:has-text('发布商品') a",
    "a:has-text('发布商品')",
)
START_BUTTON_SELECTORS = (
    "button.model-button.next-btn-primary:has-text('开启')",
    "button.next-btn-primary:has(span.next-btn-helper:has-text('开启'))",
    "button:has-text('开启')",
)
PAGE_READY_TIMEOUT_MS = 60_000
SHORT_TIMEOUT_MS = 5_000



def click_product_nav(context: BrowserContext, page: Page) -> Page:
    """点击千牛工作台的商品入口。

    参数：
        context (BrowserContext)：持久化浏览器上下文。
        page (Page)：已登录的 Playwright 页面对象。

    返回：
        Page：点击后继续操作的页面对象。
    """
    logger = get_logger(__name__)
    wait_for_page_ready(page)

    product_nav = _wait_for_product_nav(page)
    next_page = click_and_get_next_page(context, page, product_nav)
    logger.info("已点击千牛工作台商品入口")
    return next_page

def click_publish_product(context: BrowserContext, page: Page) -> Page:
    """点击商品菜单中的发布商品入口。

    参数：
        context (BrowserContext)：持久化浏览器上下文。
        page (Page)：商品菜单所在的 Playwright 页面对象。

    返回：
        Page：点击后继续操作的页面对象。
    """
    logger = get_logger(__name__)
    wait_for_page_ready(page)

    publish_product = _wait_for_publish_product(page)
    next_page = click_and_get_next_page(context, page, publish_product)
    logger.info("已点击商品菜单中的发布商品入口")
    return next_page



def _wait_for_product_nav(page: Page):
    """等待商品入口元素出现。

    参数：
        page (Page)：需要检查的 Playwright 页面对象。

    返回：
        ElementHandle：商品入口元素句柄。
    """
    last_error: Exception | None = None
    for selector in PRODUCT_NAV_SELECTORS:
        try:
            element = page.wait_for_selector(selector, state="visible", timeout=SHORT_TIMEOUT_MS)
            if element is not None:
                return element
        except PlaywrightTimeoutError as error:
            last_error = error

    raise PlaywrightTimeoutError("未检测到千牛工作台商品入口") from last_error

def _wait_for_publish_product(page: Page):
    """等待发布商品入口元素出现。

    参数：
        page (Page)：需要检查的 Playwright 页面对象。

    返回：
        ElementHandle：发布商品入口元素句柄。
    """
    last_error: Exception | None = None
    for selector in PUBLISH_PRODUCT_SELECTORS:
        try:
            element = page.wait_for_selector(selector, state="visible", timeout=SHORT_TIMEOUT_MS)
            if element is not None:
                return element
        except PlaywrightTimeoutError as error:
            last_error = error

    raise PlaywrightTimeoutError("未检测到发布商品入口") from last_error

def click_start_button(page: Page) -> None:
    """点击发布商品页面的开启按钮。

    参数：
        page (Page)：发布商品页面的 Playwright 页面对象。

    返回：
        无。
    """
    logger = get_logger(__name__)
    wait_for_page_ready(page)

    start_button = _wait_for_start_button(page)
    start_button.click()
    logger.info("已点击发布商品页面的开启按钮")

def _wait_for_start_button(page: Page):
    """等待开启按钮出现。

    参数：
        page (Page)：需要检查的 Playwright 页面对象。

    返回：
        ElementHandle：开启按钮元素句柄。
    """
    last_error: Exception | None = None
    for selector in START_BUTTON_SELECTORS:
        try:
            element = page.wait_for_selector(selector, state="visible", timeout=SHORT_TIMEOUT_MS)
            if element is not None:
                return element
        except PlaywrightTimeoutError as error:
            last_error = error

    raise PlaywrightTimeoutError("未检测到发布商品页面的开启按钮") from last_error
