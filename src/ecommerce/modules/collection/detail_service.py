"""独立详情页入口；字段确认后在同一详情页流程中接入采集。"""
from ecommerce.infrastructure.tasks.events import notify as print
import json
from importlib import import_module

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.browser.session import save_session
from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.account.taobao_service import authenticate
from ecommerce.modules.collection.detail_report import DetailCollectionReport
from ecommerce.platforms.taobao.detail import inspect_detail_entry, keep_detail_open, open_detail, resolve_detail_input
from ecommerce.platforms.taobao.session import browser_session
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention


def open_detail_input(settings, value, *, wait=True, on_attention=None, video=False, main_images=False, skus=False, parameters=False, detail_images=False, review_summary=False, reviews=False, all_reviews=False, questions=False):
    if all_reviews and not (reviews or questions):
        raise ValueError('--all 必须与 --reviews 或 --questions 一起使用')
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
            from ecommerce.modules.collection.persistence import capture_page
            capture_page(settings,record,destination,'detail_open')

        guard.perform("保存详情页入口记录", save_record)
        save_session(context, settings)
        message = f"商品 {target.item_id} 详情页入口已打开，等待 DOM 字段核对；入口记录：{destination}"
        print(message, flush=True)
        requested=dict(video=video,main_images=main_images,skus=skus,parameters=parameters,detail_images=detail_images,
                       review_summary=review_summary,reviews=reviews,questions=questions)
        steps=[('video','detail_video','collect_main_video',{'downloaded','no_video'}),
               ('main_images','detail_images','collect_main_images',{'complete'}),
               ('skus','detail_skus','collect_skus',{'complete'}),
               ('parameters','detail_parameters','collect_parameters',{'complete','no_parameters'}),
               ('detail_images','detail_description','collect_detail_images',{'complete','no_images'}),
               ('review_summary','detail_review_summary','collect_review_summary',{'complete'}),
               ('reviews','detail_reviews','collect_reviews',{'complete','no_reviews'}),
               ('questions','detail_questions','collect_questions',{'complete','no_questions'})]
        report=DetailCollectionReport(settings,target.item_id,guard.task_id,requested) if any(requested.values()) else None
        try:
            for name,module_name,function_name,accepted in steps:
                if not requested[name] or step_error is not None:
                    continue
                report.start(name)
                module=import_module('ecommerce.modules.collection.'+module_name)
                kwargs={'all_reviews':all_reviews} if name=='reviews' else {'all_questions':all_reviews} if name=='questions' else {}
                value=getattr(module,function_name)(settings,result,**kwargs)
                status=value.get('video_status') if name=='video' else value.get('status')
                allowed=module.sku_continuation_allowed(value) if name=='skus' else status in accepted
                report.finish(name,value,allowed)
                print(value['message'],flush=True)
                print(('视频采集记录：' if name=='video' else '商品详情记录：')+value['record_path'],flush=True)
                message+='；'+value['message']
                if not allowed:
                    step_error=value['message']+'；请核对本次汇总和部分结果，后续模块未执行'
            if report:
                report.finalize(step_error)
                message+='；'+report.display()
        except (Exception,KeyboardInterrupt) as error:
            if report:
                try:
                    report.interrupt(error)
                    report.display()
                except (OSError,ValueError):
                    print('本次汇总更新失败，请查看最后保存的模块进度。',flush=True)
            raise
        if wait and not settings.headless:
            keep_detail_open(result)
        if step_error:
            raise NeedsAttention(step_error)
    return message
