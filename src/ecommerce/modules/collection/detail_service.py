"""独立详情页入口；字段确认后在同一详情页流程中接入采集。"""
import json

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.browser.session import save_session
from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.account.taobao_service import authenticate
from ecommerce.platforms.taobao.detail import inspect_detail_entry, keep_detail_open, open_detail, resolve_detail_input
from ecommerce.platforms.taobao.session import browser_session
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention


def open_detail_input(settings, value, *, wait=True, on_attention=None, video=False, main_images=False, skus=False):
    target = resolve_detail_input(value)
    step_error = None
    with browser_session(settings, on_attention=on_attention) as (context, page):
        page = authenticate(context, page, settings)
        result = open_detail(page, target.url if target.input_type == "url" else target.item_id, settings.timeout_ms)
        guard = active_guard()
        guard.resume_check = lambda: inspect_detail_entry(result.page, target) is not None
        screenshot = settings.log_dir / "screenshots" / f"taobao-detail-open-{guard.task_id}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot_note = None
        while True:
            guard.checkpoint("保存详情页入口现场")
            try:
                result.page.screenshot(path=str(screenshot), timeout=5000)
            except PlaywrightError:
                screenshot_note = "详情页入口截图未完成"
                screenshot = None
            guard.checkpoint("复核详情页入口现场")
            confirmed = inspect_detail_entry(result.page, target)
            if confirmed is not None:
                result = confirmed
                break
            guard.request_attention(result.page, "保存入口记录前页面发生变化，请恢复输入商品的详情页后回复继续", step="复核详情页入口")
            screenshot = settings.log_dir / "screenshots" / f"taobao-detail-open-{guard.task_id}.png"
            screenshot_note = None
        record = {"task_id": guard.task_id, "product_id": target.item_id, "input_type": target.input_type,
                  "requested_url": target.canonical_url, "source_url": resolve_detail_input(result.page.url).canonical_url,
                  "status": "opened_pending_dom_review", "checked_at": now(), "document_title": result.document_title,
                  "screenshot": str(screenshot) if screenshot else None, "screenshot_note": screenshot_note,
                  "product_fields_collected": False}
        destination = settings.workspace / "output/raw/taobao_detail_open" / f"{guard.task_id}.json"

        def save_record():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(destination)

        guard.perform("保存详情页入口记录", save_record)
        save_session(context, settings)
        message = f"商品 {target.item_id} 详情页入口已打开，等待 DOM 字段核对；入口记录：{destination}"
        print(message, flush=True)
        if video:
            from ecommerce.modules.collection.detail_video import collect_main_video
            video_result = collect_main_video(settings, result)
            print(video_result["message"], flush=True)
            print("视频采集记录：" + video_result["record_path"], flush=True)
            message += "；" + video_result["message"]
            if video_result["video_status"] not in {"downloaded", "no_video"}:
                step_error = video_result["message"]
        if main_images and step_error is None:
            from ecommerce.modules.collection.detail_images import collect_main_images
            image_result = collect_main_images(settings, result)
            print(image_result["message"], flush=True)
            print("商品详情记录：" + image_result["record_path"], flush=True)
            message += "；" + image_result["message"]
            if image_result["status"] != "complete":
                step_error = image_result["message"]
        if skus and step_error is None:
            from ecommerce.modules.collection.detail_skus import collect_skus
            sku_result = collect_skus(settings, result)
            print(sku_result["message"], flush=True)
            print("商品详情记录：" + sku_result["record_path"], flush=True)
            message += "；" + sku_result["message"]
            if sku_result["status"] != "complete":
                step_error = sku_result["message"] + "；请核对历史批次和最后尝试记录"
        if wait and not settings.headless:
            keep_detail_open(result)
        if step_error:
            raise NeedsAttention(step_error)
    return message
