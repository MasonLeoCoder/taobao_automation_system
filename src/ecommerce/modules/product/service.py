"""商品业务流程，当前提供发布入口导航。"""
from collections.abc import Callable
from ecommerce.infrastructure.browser.session import browser_session
from ecommerce.modules.account.service import authenticate
from ecommerce.platforms.qianniu.product import click_product_nav, click_publish_product, click_start_button
from ecommerce.shared.config import Settings


def open_publish(settings: Settings, start: bool = False, on_ready: Callable[[], None] | None = None) -> str:
    """打开发布入口；参数：配置、是否点击开启、就绪回调；返回：结果说明。"""
    with browser_session(settings) as (context, page):
        page = authenticate(context, page, settings)
        page = click_product_nav(context, page)
        page = click_publish_product(context, page)
        if start:
            click_start_button(page)
        if on_ready is not None:
            on_ready()
    return "已完成发布入口导航" + ("及开启按钮操作" if start else "") + "；本任务不保存商品草稿"
