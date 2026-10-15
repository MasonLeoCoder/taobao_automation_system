"""千牛商品入口操作，迁移现有选择器。"""
from .verification import qianniu_operation, checkpoint, page_step
from logging import getLogger as get_logger
import re
from urllib.parse import urlsplit
from ecommerce.infrastructure.browser.actions import (
    click_and_get_next_page,
    wait_for_page_ready,
    wait_for_visible_element,
)
from playwright.sync_api import BrowserContext, Page, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from .popups import _dismiss_known_sku

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


def _safe_location(page: Page) -> str:
    """生成不含查询参数的诊断地址；参数：页面；返回：地址。"""
    location = urlsplit(page.url)
    return f"{location.scheme}://{location.netloc}{location.path}"


def _dismiss_sku_notice(page: Page) -> bool:
    """关闭已识别的SKU公告；参数：页面；返回：是否关闭，其他弹窗不作处理。"""
    checkpoint("关闭SKU管理页引导公告")
    return _dismiss_known_sku(page)


def _navigate(context, page, element):
    """恢复一次只读导航；参数：上下文、原页面、入口；返回：新页面，已跳转时不再点击。"""
    baseline = tuple(context.pages)
    original = page.url
    def navigate():
        opened = [current for current in context.pages if current not in baseline and not current.is_closed()]
        if opened:
            wait_for_page_ready(opened[0])
            return opened[0]
        if not page.is_closed() and page.url != original:
            wait_for_page_ready(page)
            return page
        return click_and_get_next_page(context, page, element)
    return page_step(navigate)


@qianniu_operation
def select_brand_and_continue(page: Page, brand: str) -> Page:
    """选择品牌并进入下一步；参数：类目页、完整品牌名称；返回：后续页面。"""
    if not brand.strip():
        raise ValueError("品牌不能为空")
    page_step(lambda: page.get_by_placeholder("请输入", exact=True).click(), completed=lambda: page.locator("input:focus").count() == 1)
    # 下拉框展开后，搜索输入框获得焦点，避免填写顶部的商品搜索框。
    page_step(lambda: page.locator("input:focus").fill(brand.split("/", 1)[0]))
    page_step(lambda: page.get_by_text(brand, exact=True).click(), completed=lambda: page.get_by_placeholder("请输入", exact=True).get_attribute("aria-expanded") == "false")
    get_logger(__name__).info("已选择品牌：%s", brand)
    next_button = page.get_by_role("button", name=re.compile(r"确认\s*[,，]\s*下一步"))
    next_page = _navigate(page.context, page, next_button)
    page_step(lambda: wait_for_page_ready(next_page))
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
    original_url = page.url
    page_step(lambda: page.get_by_text(pattern).click(), completed=lambda: page.url != original_url)
    page_step(lambda: wait_for_page_ready(page))
    get_logger(__name__).info("已点击类目：%s", " > ".join(parts))


@qianniu_operation
def search_publish_products(page: Page, keyword: str) -> None:
    """搜索发品；参数：发布页面、非空关键词；返回：无，等待结果页出现。"""
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("搜索关键词不能为空")
    logger = get_logger(__name__)
    logger.info("准备搜索发品：关键词=%s；操作页面=%s", keyword, _safe_location(page))
    search_input = page.locator(SEARCH_INPUT_SELECTOR)
    try:
        page_step(lambda: search_input.wait_for(state="visible"))
    except PlaywrightTimeoutError:
        try:
            controls = page.locator("input[placeholder], textarea[placeholder]").evaluate_all("els=>els.slice(0,12).map(e=>({tag:e.tagName,placeholder:e.getAttribute('placeholder'),visible:!!e.getClientRects().length}))")
            logger.error("发布搜索框定位超时：操作页=%s；打开的标签页=%s；主页面输入控件=%s；框架数量=%d", _safe_location(page), [_safe_location(current) for current in page.context.pages], controls, len(page.frames))
        except PlaywrightError:
            logger.warning("发布搜索框超时诊断失败，页面可能正在跳转或已经关闭")
        raise
    page_step(lambda: search_input.fill(keyword))
    page_step(lambda: page.get_by_role("button", name="搜索", exact=True).click(), completed=lambda: page.get_by_text("全部", exact=True).is_visible())
    # 初始类目选择页没有“全部”结果标签，等待搜索结果视图出现。
    page_step(lambda: page.get_by_text("全部", exact=True).wait_for(state="visible"))
    page_step(lambda: page.get_by_text("我的商品", exact=True).first.wait_for(state="visible"))
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
    page_step(lambda: wait_for_page_ready(page))

    product_nav = page_step(lambda: wait_for_visible_element(
        page, PRODUCT_NAV_SELECTORS, element_name="千牛工作台商品入口"
    ))
    next_page = _navigate(context, page, product_nav)
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
    page_step(lambda: wait_for_page_ready(page))
    _dismiss_sku_notice(page)

    # 点击商品菜单后鼠标仍停在菜单上，会触发悬浮菜单并遮住侧栏入口。
    # 先移到页面边缘，等待浮层收起，再查找侧栏的发布链接。
    page.mouse.move(0, 0)

    publish_product = page_step(lambda: wait_for_visible_element(
        page, PUBLISH_PRODUCT_SELECTORS, element_name="发布商品入口"
    ))
    _dismiss_sku_notice(page)
    next_page = _navigate(context, page, publish_product)
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
    page_step(lambda: wait_for_page_ready(page))

    start_button = page_step(lambda: wait_for_visible_element(
        page, START_BUTTON_SELECTORS, element_name="发布商品页面的开启按钮"
    ))
    page_step(start_button.click, completed=lambda: not start_button.is_visible())
    logger.info("已点击发布商品页面的开启按钮")
