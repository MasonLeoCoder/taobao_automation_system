"""千牛商品入口操作，迁移现有选择器。"""
from .verification import qianniu_operation, checkpoint
from logging import getLogger as get_logger
import re
from ecommerce.infrastructure.browser.actions import (
    click_and_get_next_page,
    wait_for_page_ready,
    wait_for_visible_element,
)
from playwright.sync_api import BrowserContext, Page

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
SEARCH_INPUT_SELECTOR = "input[placeholder*='可输入产品名称']"


@qianniu_operation
def select_brand_and_continue(page: Page, brand: str) -> Page:
    """选择品牌并进入下一步；参数：类目页、完整品牌名称；返回：后续页面。"""
    if not brand.strip():
        raise ValueError("品牌不能为空")
    page.get_by_placeholder("请输入", exact=True).click()
    # 下拉框展开后，搜索输入框获得焦点，避免填写顶部的商品搜索框。
    page.locator("input:focus").fill(brand.split("/", 1)[0])
    page.get_by_text(brand, exact=True).click()
    get_logger(__name__).info("已选择品牌：%s", brand)
    next_button = page.get_by_role("button", name=re.compile(r"确认\s*[,，]\s*下一步"))
    next_page = click_and_get_next_page(page.context, page, next_button)
    wait_for_page_ready(next_page)
    get_logger(__name__).info("已点击确认下一步，等待检查后续页面")
    return next_page


@qianniu_operation
def select_publish_category(page: Page, category: str) -> None:
    """点击完整类目路径；参数：结果页、以 > 分隔的路径；返回：无。"""
    parts = [part.strip() for part in category.split(">")]
    if len(parts) < 2 or not all(parts):
        raise ValueError("请提供以 > 分隔的完整类目路径")
    # 按完整路径唯一匹配，避免同名末级类目或搜索排序变化导致误选。
    pattern = re.compile(r"^\s*" + r"\s*>?\s*".join(re.escape(part) for part in parts) + r"\s*$")
    page.get_by_text(pattern).click()
    wait_for_page_ready(page)
    get_logger(__name__).info("已点击类目：%s", " > ".join(parts))


@qianniu_operation
def search_publish_products(page: Page, keyword: str) -> None:
    """搜索发品；参数：发布页面、非空关键词；返回：无，等待结果页出现。"""
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("搜索关键词不能为空")
    logger = get_logger(__name__)
    search_input = page.locator(SEARCH_INPUT_SELECTOR)
    search_input.wait_for(state="visible")
    search_input.fill(keyword)
    page.get_by_role("button", name="搜索", exact=True).click()
    # 初始类目选择页没有“全部”结果标签，等待搜索结果视图出现。
    page.get_by_text("全部", exact=True).wait_for(state="visible")
    page.get_by_text("我的商品", exact=True).first.wait_for(state="visible")
    logger.info("发品搜索结果页已显示，等待下一步操作")


@qianniu_operation
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

    product_nav = wait_for_visible_element(
        page, PRODUCT_NAV_SELECTORS, element_name="千牛工作台商品入口"
    )
    next_page = click_and_get_next_page(context, page, product_nav)
    logger.info("已点击千牛工作台商品入口")
    return next_page

@qianniu_operation
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

    # 点击商品菜单后鼠标仍停在菜单上，会触发悬浮菜单并遮住侧栏入口。
    # 先移到页面边缘，等待浮层收起，再查找侧栏的发布链接。
    page.mouse.move(0, 0)
    page.locator(".next-overlay-wrapper.opened:visible").wait_for(
        state="hidden", timeout=5_000
    )

    publish_product = wait_for_visible_element(
        page, PUBLISH_PRODUCT_SELECTORS, element_name="发布商品入口"
    )
    next_page = click_and_get_next_page(context, page, publish_product)
    logger.info("已点击商品菜单中的发布商品入口")
    return next_page


@qianniu_operation
def click_start_button(page: Page) -> None:
    """点击发布商品页面的开启按钮。

    参数：
        page (Page)：发布商品页面的 Playwright 页面对象。

    返回：
        无。
    """
    logger = get_logger(__name__)
    wait_for_page_ready(page)

    start_button = wait_for_visible_element(
        page, START_BUTTON_SELECTORS, element_name="发布商品页面的开启按钮"
    )
    start_button.click()
    logger.info("已点击发布商品页面的开启按钮")
