"""商品业务流程，当前提供发布入口导航。"""
from collections.abc import Callable
from logging import getLogger
from uuid import uuid4
from pathlib import Path
from ecommerce.modules.product.assets import load_main_images, load_portrait_images, load_detail_images, load_product_video
from ecommerce.platforms.qianniu.product_images import upload_main_images, upload_portrait_images
from ecommerce.modules.product.workbook import load_product_workbook
from ecommerce.platforms.qianniu.product_attributes import select_applicable_scene, select_pet_type, select_handle_shape, select_adhesive_material, select_adhesion_strength
from ecommerce.platforms.qianniu.product_basic import fill_product_title, fill_guide_title, fill_fixed_price, fill_merchant_code, enable_multi_item_discount, select_warehouse_listing
from ecommerce.platforms.qianniu.product_shop_category import select_shop_categories
from ecommerce.platforms.qianniu.product_sku import create_sales_specifications, fill_sku_details
from ecommerce.platforms.qianniu.product_details import upload_detail_images
from ecommerce.platforms.qianniu.product_submit import SubmissionResult, submit_product_to_warehouse, inspect_submission_result
from ecommerce.platforms.qianniu.session import browser_session
from ecommerce.modules.account.service import authenticate
from ecommerce.platforms.qianniu.product import click_product_nav, click_publish_product, click_start_button, search_publish_products, select_publish_category, select_brand_and_continue
from ecommerce.shared.config import Settings
from ecommerce.shared.exceptions import NeedsAttention


def submit_with_review(settings: Settings, page, on_review: Callable[[str], None] | None = None) -> str:
    """提交并等待人工检查；参数：配置、商品页、人工回调；返回：经平台页面确认的成功说明。"""
    baseline_pages = tuple(page.context.pages)
    result = submit_product_to_warehouse(page)
    while not result.submit_attempted and result.status != "succeeded":
        _request_submission_review(settings, result, "提交前检查未通过（尚未点击提交）", result.message,
                                   "请人工修正页面后反馈“继续”。未收到反馈前不会点击提交；反馈后先重新检查，检查通过才提交一次。",
                                   on_review)
        result = submit_product_to_warehouse(page)
    return review_submission_result(settings, page, result, on_review, baseline_pages)


def _request_submission_review(settings: Settings, result: SubmissionResult, label: str,
                               details: str, instruction: str,
                               on_review: Callable[[str], None] | None) -> None:
    """记录并提醒人工检查；参数：配置、结果、原因、操作说明、回调；返回：明确反馈后返回。"""
    logger = getLogger(__name__)
    logger.error("%s：%s", label, details)
    screenshot = settings.log_dir / "screenshots" / f"submit-review-{uuid4().hex}.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    try:
        result.page.screenshot(path=str(screenshot), timeout=5000)
        screenshot_note = f"截图：{screenshot}"
    except Exception:
        logger.exception("商品提交检查截图保存失败")
        screenshot_note = "截图保存失败，请直接检查浏览器"
    message = f"【需要人工检查】{label}：{details}\n{screenshot_note}\n{instruction}"
    if on_review is None:
        raise NeedsAttention(message)
    on_review(message)


def review_submission_result(settings: Settings, page, result: SubmissionResult,
                            on_review: Callable[[str], None] | None = None,
                            baseline_pages: tuple | None = None) -> str:
    """核对已有提交结果并提醒人工；参数：配置、原页、结果、回调、原页面集；返回：如实说明，不点击提交。"""
    baseline_pages = tuple(page.context.pages) if baseline_pages is None else baseline_pages
    while result.status != "succeeded":
        label = "提交后检测到错误" if result.status == "failed" else "提交结果未确认"
        _request_submission_review(settings, result, label, result.message,
                                   "提交操作已经尝试，千牛可能仍在处理。请先核对结果页及仓库，避免重复提交。程序等待明确反馈，反馈后只复核结果。",
                                   on_review)
        result = inspect_submission_result(page, baseline_pages)
    message = result.message + (f"；商品ID：{result.item_id}" if result.item_id else "")
    logger = getLogger(__name__)
    logger.info("商品提交成功已核对：%s", message)
    if result.issues:
        _request_submission_review(settings, result, "商品已提交，但平台提示异常",
                                   message + "；" + "；".join(result.issues),
                                   "商品已经提交成功，请人工检查上述问题，无需再次提交商品。程序等待明确反馈，反馈后只复核提示。",
                                   on_review)
        checked = inspect_submission_result(result.page, tuple(result.page.context.pages))
        if checked.status == "succeeded":
            if checked.issues:
                logger.warning("已收到人工检查反馈，平台仍显示：%s", "；".join(checked.issues))
                return message + "；已收到人工检查反馈，平台仍提示：" + "；".join(checked.issues)
            return message
        logger.warning("人工检查反馈后结果页不可确认；保留此前已确认的提交结果：%s", checked.message)
        return message + "；已收到人工检查反馈，平台异常处理结果待人工核对：" + "；".join(result.issues)
    return message


def open_publish(settings: Settings, start: bool = False, on_ready: Callable[[], None] | None = None, keyword: str | None = None, category: str | None = None, brand: str | None = None, main_images_dir: str | None = None, portrait_images_dir: str | None = None, on_attention: Callable[[str], None] | None = None, detail_images_dir: str | None = None, video_file: str | None = None, product_info_file: str | None = None, submit: bool = False, on_submit_attention: Callable[[str], None] | None = None) -> str:
    """打开发布入口；参数：配置、开启开关、就绪回调、搜索词、完整类目、品牌、1:1主图目录、3:4主图目录、人工处理回调、详情图目录；返回：结果说明。"""
    if submit and (brand is None or product_info_file is None):
        raise ValueError("提交需要提供品牌和商品Excel，以完成填写并选择放入仓库")
    submission_message = None
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
    video_path = None
    if video_file is not None:
        if brand is None:
            raise ValueError("上传商品视频需要提供品牌并进入商品填写页")
        path = Path(video_file)
        video_path = load_product_video(path if path.is_absolute() else settings.root / path)
    product_info = None
    shop_categories = None
    category_result = None
    scene_result = None
    pet_type_result = None
    handle_shape_result = None
    adhesive_material_result = None
    adhesion_strength_result = None
    product_title = None
    guide_title = None
    sales_specifications = None
    sku_details = None
    fixed_price = None
    merchant_code = None
    if product_info_file is not None:
        if brand is None:
            raise ValueError("填写店铺分类需要提供品牌并进入商品填写页")
        path = Path(product_info_file)
        product_info = load_product_workbook(path if path.is_absolute() else settings.root / path)
        shop_categories = product_info.shop_categories()
        product_title = product_info.text("宝贝标题")
        guide_title = product_info.text("导购标题")
        sales_specifications = product_info.sales_specifications()
        sku_details = product_info.sku_details()
        fixed_price = product_info.fixed_price()
        merchant_code = product_info.text("商家编码")
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
        if shop_categories is not None:
            category_result = select_shop_categories(page, shop_categories)
        if video_path is not None:
            from ecommerce.platforms.qianniu.product_video import upload_product_video
            upload_product_video(page, video_path)
        if product_title is not None:
            fill_product_title(page, product_title)
        if guide_title is not None:
            fill_guide_title(page, guide_title)
        if product_info is not None:
            scene_result = select_applicable_scene(page, product_info.fields.get("适用场景"))
            pet_type_result = select_pet_type(page, product_info.fields.get("适用宠物类型"))
            handle_shape_result = select_handle_shape(page, product_info.fields.get("握柄形态"))
            adhesive_material_result = select_adhesive_material(page, product_info.fields.get("粘面材质"))
            adhesion_strength_result = select_adhesion_strength(page, product_info.fields.get("粘性强度"))
            create_sales_specifications(page, sales_specifications)
            fill_sku_details(page, sku_details)
            fill_fixed_price(page, fixed_price)
            fill_merchant_code(page, merchant_code)
            enable_multi_item_discount(page)
            select_warehouse_listing(page)
        if category is not None:
            destination = settings.log_dir / "screenshots" / f"{'title' if product_title else 'video' if video_path else 'details' if detail_images else 'main-images' if images or portrait_images else 'category'}-{uuid4().hex}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(destination))
        if submit:
            submission_message = submit_with_review(settings, page, on_submit_attention)
        if on_ready is not None:
            on_ready()
    return "已完成发布入口导航" + ("及开启按钮操作" if start else "") + ("及发品搜索" if keyword is not None else "") + ("及类目点击" if category is not None else "") + ("及品牌确认下一步" if brand is not None else "") + ("及五张1:1主图填写" if images else "") + ("及五张3:4主图填写" if portrait_images else "") + (f"及{len(detail_images)}张详情图填写" if detail_images else "") + ("及商品视频上传" if video_path else "") + (f"；店铺分类匹配：已选{len(category_result.selected)}项，跳过{len(category_result.skipped)}项" if category_result is not None else "") + ("及宝贝标题填写" if product_title else "") + ("及导购标题填写" if guide_title else "") + (f"；适用场景={scene_result.selected}" + ("（默认第一项）" if scene_result.fallback_reason else "") if scene_result is not None else "") + (f"；适用宠物类型={pet_type_result.selected}" + ("（默认第一项）" if pet_type_result.fallback_reason else "") if pet_type_result is not None else "") + (f"；握柄形态={handle_shape_result.selected}" + ("（默认第一项）" if handle_shape_result.fallback_reason else "") if handle_shape_result is not None else "") + (f"；粘面材质={adhesive_material_result.selected}" + ("（默认第一项）" if adhesive_material_result.fallback_reason else "") if adhesive_material_result is not None else "") + (f"；粘性强度={adhesion_strength_result.selected}" + ("（默认第一项）" if adhesion_strength_result.fallback_reason else "") if adhesion_strength_result is not None else "") + (f"；销售规格={len(sales_specifications)}项" if sales_specifications is not None else "") + (f"；SKU明细={len(sku_details)}项" if sku_details is not None else "") + (f"；一口价={fixed_price}元" if fixed_price is not None else "") + (f"；商品级商家编码={merchant_code}" if merchant_code is not None else "") + ("；多件优惠已启用" if product_info is not None else "") + ("；上架时间=放入仓库" if product_info is not None else "") + (f"；商品提交已确认：{submission_message}" if submission_message is not None else "；未提交发布")
