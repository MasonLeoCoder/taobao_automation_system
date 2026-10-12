"""商品业务流程，当前提供发布入口导航。"""
from collections.abc import Callable
from uuid import uuid4
from ecommerce.infrastructure.browser.session import browser_session
from ecommerce.modules.account.service import authenticate
from ecommerce.platforms.qianniu.product import click_product_nav, click_publish_product, click_start_button, search_publish_products, select_publish_category, select_brand_and_continue
from ecommerce.shared.config import Settings


def open_publish(settings: Settings, start: bool = False, on_ready: Callable[[], None] | None = None, keyword: str | None = None, category: str | None = None, brand: str | None = None) -> str:
    """打开发布入口；参数：配置、开启开关、就绪回调、搜索词、完整类目、品牌；返回：结果说明。"""
    if keyword is not None and not keyword.strip():
        raise ValueError("搜索关键词不能为空")
    if category is not None:
        if keyword is None:
            raise ValueError("选择搜索类目前必须提供搜索关键词")
        parts = category.split(">")
        if len(parts) < 2 or not all(part.strip() for part in parts):
            raise ValueError("请提供以 > 分隔的完整类目路径")
    if brand is not None and (not brand.strip() or category is None):
        raise ValueError("选择品牌需要提供完整类目及非空品牌")
    with browser_session(settings) as (context, page):
        page = authenticate(context, page, settings)
        page = click_product_nav(context, page)
        page = click_publish_product(context, page)
        if start:
            click_start_button(page)
        if keyword is not None:
            search_publish_products(page, keyword)
        if category is not None:
            select_publish_category(page, category)
        if brand is not None:
            page = select_brand_and_continue(page, brand)
        if category is not None:
            destination = settings.log_dir / "screenshots" / f"category-{uuid4().hex}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(destination))
        if on_ready is not None:
            on_ready()
    return "已完成发布入口导航" + ("及开启按钮操作" if start else "") + ("及发品搜索" if keyword is not None else "") + ("及类目点击" if category is not None else "") + ("及品牌确认下一步" if brand is not None else "") + "；本任务不保存商品草稿"
