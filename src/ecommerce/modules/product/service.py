"""商品业务流程，当前提供发布入口导航。"""
from collections.abc import Callable
from uuid import uuid4
from pathlib import Path
from ecommerce.modules.product.assets import load_main_images, load_portrait_images, load_detail_images
from ecommerce.platforms.qianniu.product_images import upload_main_images, upload_portrait_images
from ecommerce.platforms.qianniu.product_details import upload_detail_images
from ecommerce.platforms.qianniu.session import browser_session
from ecommerce.modules.account.service import authenticate
from ecommerce.platforms.qianniu.product import click_product_nav, click_publish_product, click_start_button, search_publish_products, select_publish_category, select_brand_and_continue
from ecommerce.shared.config import Settings


def open_publish(settings: Settings, start: bool = False, on_ready: Callable[[], None] | None = None, keyword: str | None = None, category: str | None = None, brand: str | None = None, main_images_dir: str | None = None, portrait_images_dir: str | None = None, on_attention: Callable[[str], None] | None = None, detail_images_dir: str | None = None) -> str:
    """打开发布入口；参数：配置、开启开关、就绪回调、搜索词、完整类目、品牌、1:1主图目录、3:4主图目录、人工处理回调、详情图目录；返回：结果说明。"""
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
    images = None
    if main_images_dir is not None:
        if brand is None:
            raise ValueError("上传主图需要提供品牌并进入商品填写页")
        directory = Path(main_images_dir)
        images = load_main_images(directory if directory.is_absolute() else settings.root / directory)
    portrait_images = None
    if portrait_images_dir is not None:
        if brand is None:
            raise ValueError("上传3:4主图需要提供品牌并进入商品填写页")
        directory = Path(portrait_images_dir)
        portrait_images = load_portrait_images(directory if directory.is_absolute() else settings.root / directory)
    detail_images = None
    if detail_images_dir is not None:
        if brand is None:
            raise ValueError("上传详情图需要提供品牌并进入商品填写页")
        directory = Path(detail_images_dir)
        detail_images = load_detail_images(directory if directory.is_absolute() else settings.root / directory)
    with browser_session(settings, on_attention=on_attention) as (context, page):
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
        if images is not None:
            if on_attention is None:
                upload_main_images(page, images)
            else:
                upload_main_images(page, images, on_attention=on_attention)
        if portrait_images is not None:
            if on_attention is None:
                upload_portrait_images(page, portrait_images)
            else:
                upload_portrait_images(page, portrait_images, on_attention=on_attention)
        if detail_images is not None:
            upload_detail_images(page, detail_images)
        if category is not None:
            destination = settings.log_dir / "screenshots" / f"{'details' if detail_images else 'main-images' if images or portrait_images else 'category'}-{uuid4().hex}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(destination))
        if on_ready is not None:
            on_ready()
    return "已完成发布入口导航" + ("及开启按钮操作" if start else "") + ("及发品搜索" if keyword is not None else "") + ("及类目点击" if category is not None else "") + ("及品牌确认下一步" if brand is not None else "") + ("及五张1:1主图填写" if images else "") + ("及五张3:4主图填写" if portrait_images else "") + (f"及{len(detail_images)}张详情图填写" if detail_images else "") + "；未提交发布"
