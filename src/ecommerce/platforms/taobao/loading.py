"""同页分段滚动触发显示主图的延迟加载，不推测隐藏图片的格式。"""
from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.cards import SNAPSHOT, parse_cards
from ecommerce.platforms.taobao.verification import active_guard


def prepare_list_images(result, limit, timeout_ms, *, page_number=1):
    """加载目标页主图；参数：搜索页、每页上限、超时、页码；返回：加载统计。"""
    guard = active_guard()
    if guard is None:
        raise RuntimeError("主图加载必须在淘宝守卫中运行")
    page = result.page
    stats = {"scroll_count": 0, "missing_before": 0, "missing_after": 0}
    if result.status == "empty":
        return stats

    def read():
        raw = page.evaluate(SNAPSHOT)
        return raw, parse_cards(raw, result.keyword, limit, page_number=page_number)

    def ready():
        try:
            raw, parsed = read()
            return parsed["signature"]["sort_label"] == "销量" and not raw["list_busy"]
        except (PlaywrightError, ValueError):
            return False

    guard.resume_check = ready
    try:
        raw, parsed = guard.perform("检查前列商品主图加载", read)
    except ValueError:
        return stats  # 正式解析步骤负责记录结构异常及转人工。
    if parsed["blocking_issues"] or not parsed["items"]:
        return stats
    signature = parsed["signature"]
    identity = [(r["slot_index"], r["product_id"], r["title"], r["price_text"]) for r in parsed["items"]]
    missing = [r for r in parsed["items"] if not r["main_image_url"]]
    stats["missing_before"] = len(missing)
    idle = 0
    while missing and stats["scroll_count"] < 8 and idle < 4:
        if not ready():
            guard.request_attention(page, f"加载主图时页面条件改变，请核对原关键词、第{page_number}页及销量排序后继续", step="复核主图加载页面")
        target = missing[0]["slot_index"]
        geometry = page.evaluate("""position=>{
          const e=document.querySelector('#content_items_wrapper')?.children[position-1];
          if(!e)return null;const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:innerHeight,y:scrollY};
        }""", target)
        if not geometry:
            break
        # 卡片刚露边时懒加载阈值可能尚未达到，让主图区进入视口上半部。
        if geometry["top"] >= geometry["height"] * .45 or geometry["top"] < 0:
            direction = 1 if geometry["top"] >= geometry["height"] * .45 else -1
            distance = direction * max(200, geometry["height"] * .8)
            guard.perform("同页滚动加载商品主图", lambda: page.mouse.wheel(0, distance))
            stats["scroll_count"] += 1
            guard.pause_between("scroll")
        else:
            idle += 1
            guard.wait(min(500, timeout_ms))
        raw, parsed = guard.perform("核对延迟加载后的商品", read)
        current = [(r["slot_index"], r["product_id"], r["title"], r["price_text"]) for r in parsed["items"]]
        if parsed["blocking_issues"] or parsed["signature"] != signature or current != identity:
            guard.request_attention(page, "加载图片期间商品列表发生变化，请人工核对后继续，将按当前列表重新读取", step="复核主图加载期间列表")
            raw, parsed = read()
            signature, identity = parsed["signature"], [(r["slot_index"], r["product_id"], r["title"], r["price_text"]) for r in parsed["items"]]
            if parsed["blocking_issues"]:
                break
        updated = [r for r in parsed["items"] if not r["main_image_url"]]
        if len(updated) < len(missing):
            idle = 0
        missing = updated
    stats["missing_after"] = len(missing)
    return stats
