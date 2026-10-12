"""共用浏览器等待和元素查找方法。"""
import random
import time
from playwright.sync_api import BrowserContext, Page, ElementHandle, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
PAGE_TIMEOUT_MS = 60000
SHORT_TIMEOUT_MS = 5000

def wait_for_visible_element(
    page: Page,
    selectors: tuple[str, ...],
    *,
    element_name: str,
    timeout: int = SHORT_TIMEOUT_MS,
) -> ElementHandle:
    """按顺序尝试定位规则，返回主页面中首个命中的可见元素。

    element_name 用于失败提示；timeout 是每条规则的等待时间（毫秒），
    不是所有规则共用的总时限。仅在超时时尝试下一条规则，其他错误直接抛出。
    """
    last_error: Exception | None = None
    for selector in selectors:
        try:
            element = page.wait_for_selector(selector, state="visible", timeout=timeout)
            if element is not None:
                return element
        except PlaywrightTimeoutError as error:
            last_error = error

    raise PlaywrightTimeoutError(f"未检测到{element_name}") from last_error

def wait_for_page_ready(page: Page) -> None:
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

def has_visible_selector(page: Page, selectors: tuple[str, ...], timeout: int) -> bool:
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
            wait_for_selector_any_frame(page, selector, state="visible", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            continue
    return False

def wait_for_selector_any_frame(
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

def human_delay(page: Page) -> None:
    """加入随机等待，降低页面未稳定时操作的概率。

    参数：
        page (Page)：执行等待的页面对象。

    返回：
        None：该函数只负责等待，不返回业务数据。
    """
    delay_ms = random.randint(400, 1_200)
    page.wait_for_timeout(delay_ms)


def click_and_get_next_page(context: BrowserContext, page: Page, element) -> Page:
    """点击元素并返回点击后的操作页面。

    参数：
        context (BrowserContext)：持久化浏览器上下文。
        page (Page)：点击前的 Playwright 页面对象。
        element：需要点击的元素句柄。

    返回：
        Page：如果点击打开新页面则返回新页面，否则返回原页面。
    """
    clicked = False
    try:
        with context.expect_page(timeout=SHORT_TIMEOUT_MS) as page_info:
            element.click()
            clicked = True
        next_page = page_info.value
        next_page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        next_page.bring_to_front()
        return next_page
    except PlaywrightTimeoutError:
        if not clicked:
            raise
        page.wait_for_timeout(1_000)
        return page
