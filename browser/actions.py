"""浏览器执行层：封装低层级 Playwright 操作。"""

from typing import Any

from utils.retry import retry
from utils.timing import human_delay


@retry()
def goto_page(page: Any, url: str) -> None:
    """使用 Playwright 打开指定页面。

    参数：
        page: Playwright 页面对象。
        url: 目标页面地址。

    返回：
        无。
    """
    page.goto(url)
    human_delay()


@retry()
def wait_and_click(page: Any, selector: str) -> None:
    """等待页面元素出现后点击。

    参数：
        page: Playwright 页面对象。
        selector: 需要点击的稳定选择器。

    返回：
        无。
    """
    page.wait_for_selector(selector)
    page.click(selector)
    human_delay()


@retry()
def wait_and_fill(page: Any, selector: str, value: str) -> None:
    """等待页面元素出现后输入文本。

    参数：
        page: Playwright 页面对象。
        selector: 需要输入内容的稳定选择器。
        value: 要输入的文本内容。

    返回：
        无。
    """
    page.wait_for_selector(selector)
    page.fill(selector, value)
    human_delay()
