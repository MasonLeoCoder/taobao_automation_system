"""业务流程层：页面导航流程。"""

from typing import Any

from browser.actions import goto_page


def navigate_to_page(page: Any, url: str) -> None:
    """导航到指定业务页面。

    参数：
        page: Playwright 页面对象。
        url: 目标页面地址。

    返回：
        无。
    """
    goto_page(page, url)
