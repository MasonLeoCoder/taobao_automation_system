"""关键词搜索和第一页字段采集，复用淘宝独立登录和任务级守卫。"""
import json
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.browser.session import save_session
from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.account.taobao_service import authenticate
from ecommerce.platforms.taobao.search import canonical_search_url, inspect_search, keep_search_open, open_search, validate_keyword
from ecommerce.platforms.taobao.session import browser_session
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention


def search_keyword(settings, keyword, *, wait=True, on_attention=None):
    keyword = validate_keyword(keyword)
    with browser_session(settings, on_attention=on_attention) as (context, home):
        home = authenticate(context, home, settings)
        result = open_search(context, home, keyword, settings.timeout_ms)
        guard = active_guard()
        guard.checkpoint("保存关键词搜索记录")
        destination = settings.workspace / "output/raw/taobao_search" / f"{guard.task_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        screenshot = settings.log_dir / "screenshots" / f"taobao-search-{guard.task_id}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot_note = None
        try:
            result.page.screenshot(path=str(screenshot), timeout=5000)
        except PlaywrightError:
            screenshot = None
            screenshot_note = "页面截图未能完成，搜索状态按页面内容复核"
        guard.checkpoint("复核搜索记录")
        confirmed = inspect_search(result.page, keyword)
        if confirmed is None:
            raise NeedsAttention("保存前搜索页面已发生变化，未保存为成功结果")
        result = confirmed
        summary = {"task_id": guard.task_id, "keyword": keyword, "status": result.status,
                   "visible_items": result.visible_items, "source_url": canonical_search_url(keyword),
                   "checked_at": now(), "screenshot": str(screenshot) if screenshot else None,
                   "screenshot_note": screenshot_note}
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
        save_session(context, settings)
        if wait and not settings.headless:
            keep_search_open(context, result)
    label = "首屏商品列表已加载" if result.status == "results" else "页面明确显示无搜索结果"
    return f"淘宝关键词“{keyword}”搜索已确认：{label}；记录：{destination}"


def collect_from_search(settings, context, result, *, limit=None):
    """按格子读取第一页，必要时同页滚动加载主图，不进入详情。"""
    from ecommerce.modules.collection.export import save_batch
    from ecommerce.platforms.taobao.cards import SNAPSHOT, page_signature, parse_cards, validate_limit
    from ecommerce.platforms.taobao.sorting import sort_by_sales
    from ecommerce.platforms.taobao.loading import prepare_list_images

    validate_limit(limit)
    guard = active_guard()
    if guard is None:
        raise RuntimeError("商品采集必须在淘宝专用守卫中运行")
    result = sort_by_sales(result, settings.timeout_ms)
    image_loading = prepare_list_images(result, limit, settings.timeout_ms)
    keyword = validate_keyword(result.keyword)
    guard.update_progress(keyword=keyword, page_number=1, collected=0, target=limit)

    def snapshot():
        captured = now()
        return captured, result.page.evaluate(SNAPSHOT)

    def ready(signature=None, slots=None):
        try:
            value = result.page.evaluate(SNAPSHOT)
            parsed = parse_cards(value, keyword, limit)
            current = inspect_search(result.page, keyword) if result.status == "empty" and not value["cards"] else None
            valid = bool(parsed["items"]) or (current is not None and current.status == "empty")
            return (valid and not value["list_busy"] and not parsed["blocking_issues"] and (not parsed["items"] or parsed["signature"]["sort_label"] == "销量")
                    and (signature is None or parsed["signature"] == signature) and (slots is None or value["slots"] == slots))
        except (PlaywrightError, ValueError):
            return False

    def read_valid(expected=None):
        while True:
            captured, raw_value = guard.perform("读取第一页已加载商品", snapshot)
            try:
                parsed_value = parse_cards(raw_value, keyword, limit)
                if parsed_value["blocking_issues"]:
                    details = "；".join(f"格子{issue['position']}：{issue['reason']}" for issue in parsed_value["blocking_issues"][:5])
                    raise ValueError("采集范围内存在未知或破损卡片，不能静默略过：" + details)
                if parsed_value["items"] and parsed_value["signature"]["sort_label"] != "销量":
                    raise ValueError("当前未确认销量排序，请恢复销量选中状态")
                current = inspect_search(result.page, keyword) if not parsed_value["items"] else None
                if not parsed_value["items"] and not (result.status == "empty" and current is not None and current.status == "empty" and not raw_value["cards"]):
                    raise ValueError("未能识别可采集的商品标题和价格，不能记录为零商品")
                if expected is not None and parsed_value["signature"] != expected:
                    raise ValueError("当前关键词、页码或排序与开始采集时不一致")
                return captured, raw_value, parsed_value
            except ValueError as error:
                evidence = settings.log_dir / "taobao" / f"{guard.task_id}-dom-diagnostic.json"
                evidence.parent.mkdir(parents=True, exist_ok=True)
                evidence.write_text(json.dumps(raw_value, ensure_ascii=False, indent=2), encoding="utf-8")
                guard.resume_check = lambda: ready(expected)
                guard.request_attention(result.page, str(error) + "；请核对第一页和搜索条件，不会自动重新搜索", step="检查商品列表解析")

    captured_at, raw, parsed = read_valid()
    signature = parsed["signature"]
    guard.resume_check = lambda: ready(signature)
    _, after = guard.perform("复核商品列表采集条件", snapshot)
    try:
        unchanged = page_signature(after, keyword) == signature and after["slots"] == raw["slots"]
    except ValueError:
        unchanged = False
    if not unchanged:
        guard.request_attention(result.page, "采集期间关键词、页码、排序或列表内容发生变化，请核对后继续，将重新读取当前页", step="复核采集条件")
        # 明确继续后重新读取同一页快照，不重搜或拼接旧排序的商品。
        captured_at, raw, parsed = read_valid(signature)

    items = parsed["items"]
    batch_id = guard.task_id + "-" + uuid4().hex[:8]
    screenshot_path = settings.log_dir / "screenshots" / f"taobao-collection-{batch_id}.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        screenshot, screenshot_note = screenshot_path, None
        try:
            guard.checkpoint("保存商品采集现场")
            result.page.screenshot(path=str(screenshot), timeout=5000)
        except PlaywrightError:
            screenshot = None
            screenshot_note = "现场截图未完成，商品信息来自已复核的页面快照"
        guard.checkpoint("复核商品采集现场")
        if guard.perform("保存前核对关键词页码排序和列表", lambda: ready(signature, raw["slots"])):
            break
        guard.request_attention(result.page, "保存前页面条件变化，请恢复原关键词、第一页和排序后继续", step="检查保存前页面条件")
        captured_at, raw, parsed = read_valid(signature)
        items = parsed["items"]
    positions = {item["position"] for item in items}
    payload = {
        "schema_version": 2, "extraction_method": "content_items_wrapper_slots_v1", "batch_id": batch_id, "task_id": guard.task_id,
        "keyword": keyword, "page_number": 1, "sort_label": signature["sort_label"],
        "sort_parameter": signature["sort_parameter"], "captured_at": captured_at, "saved_at": now(),
        "source_url": canonical_search_url(keyword), "scope": "first_page_loaded_cards",
        "requested_limit": limit, "collected_count": len(items), "loaded_card_count": parsed["loaded_card_count"],
        "duplicates_removed": parsed["duplicates"], "skipped": parsed["skipped"],
        "counts": parsed["counts"], "issues": parsed["issues"], "repeated_positions_kept": True,
        "field_gap_count": parsed["field_gap_count"],
        "collection_status": ("empty" if not items else "all_loaded_cards_collected" if limit is None
                              else "limit_reached" if parsed["limit_reached"] else "loaded_cards_below_limit"),
        "sort_identified": signature["sort_label"] is not None or signature["sort_parameter"] is not None,
        "requested_sort": "sales", "sort_status": "confirmed" if signature["sort_label"] == "销量" else "not_applicable_empty_results",
        "items": items, "raw_items": [card for card in raw["cards"] if card["position"] in positions],
        "pagination_performed": False, "scroll_performed": image_loading["scroll_count"] > 0, "image_loading": image_loading,
        "detail_pages_opened": 0,
        "screenshot": str(screenshot) if screenshot else None, "screenshot_note": screenshot_note,
    }
    guard.update_progress(collected=len(items))
    paths = guard.perform("保存当前页商品采集批次", lambda: save_batch(
        settings.workspace / "output/raw/taobao_products", payload))
    guard.checkpoint("商品采集记录已保存")
    save_session(context, settings)
    message = (f"关键词“{keyword}”销量第一页已保存：{len(items)} 条商品展示记录"
               f"（其中广告推荐位 {parsed['counts']['exported_ad_count']} 条，列表跳过调查卡 {parsed['counts']['survey_count']} 个、"
               f"推荐词卡 {parsed['counts']['search_suggestions_count']} 个）；"
               f"JSON：{paths[0]}；CSV：{paths[1]}")
    return {"message": message, "json_path": str(paths[0]), "csv_path": str(paths[1]),
            "collected_count": len(items), "field_gap_count": parsed["field_gap_count"]}


def collect_keyword(settings, keyword, *, limit=None, wait=True, on_attention=None):
    from ecommerce.platforms.taobao.cards import validate_limit

    keyword = validate_keyword(keyword)
    validate_limit(limit)
    with browser_session(settings, on_attention=on_attention) as (context, home):
        home = authenticate(context, home, settings)
        result = open_search(context, home, keyword, settings.timeout_ms)
        collected = collect_from_search(settings, context, result, limit=limit)
        print(collected["message"], flush=True)
        if wait and not settings.headless:
            keep_search_open(context, result)
    return collected["message"]
