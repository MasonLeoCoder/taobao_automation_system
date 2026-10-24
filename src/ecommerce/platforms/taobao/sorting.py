"""点击淘宝列表的销量页签并只读确认更新；阻断或超时交给人工。"""
from ecommerce.infrastructure.tasks.events import notify as print
import re
import time

from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.cards import CARD_SELECTOR, SNAPSHOT, page_signature
from ecommerce.platforms.taobao.search import inspect_search
from ecommerce.platforms.taobao.verification import active_guard

SALES_TABS = '.next-tabs-nav .next-tabs-tab[class*="customTabItem--"]'


def _fingerprint(raw):
    return [(card.get("dom_item_id"), card.get("title"), card.get("price_number_text"),
             tuple((card.get("link") or {}).get("ids", []))) for card in raw["cards"]]


def sort_by_sales(result, timeout_ms):
    """一次排序点击；已选销量则复核复用，明确无结果则无需排序。"""
    guard = active_guard()
    if guard is None:
        raise RuntimeError("销量排序必须在淘宝专用守卫中运行")
    page, keyword = result.page, result.keyword
    guard.update_progress(keyword=keyword, page_number=1)

    def read():
        try:
            raw = page.evaluate(SNAPSHOT)
            signature = page_signature(raw, keyword)
            return raw, signature, inspect_search(page, keyword)
        except (PlaywrightError, ValueError):
            return None

    def valid(state, *, sales=False):
        return bool(state and state[2] is not None and not state[0]["list_busy"]
                    and (not sales or (state[1]["sort_label"] == "销量" and state[0]["sales_control_count"] == 1)))

    def usable(*, sales=False):
        return valid(read(), sales=sales)

    def is_entry(state):
        return valid(state) and (state[2].status == "empty" or (
            state[0]["sales_control_count"] == 1 and state[0]["sales_control_enabled"]))

    def entry_ready():
        return is_entry(read())

    def wait_entry(reason, step):
        while True:
            guard.checkpoint(step)
            state = read()
            if is_entry(state):
                return state
            guard.request_attention(page, reason, step=step)

    guard.resume_check = entry_ready
    initial = wait_entry("未能确认第一页的唯一可用销量入口，请核对关键词和页面；不会自动重搜", "检查销量排序入口")
    if initial[2].status == "empty":
        return initial[2]
    guard.pause_between("before_search")
    before, signature, confirmed = wait_entry("排序前页面条件发生变化，请恢复原关键词第一页", "复核销量排序条件")
    if confirmed.status == "empty":
        return confirmed
    already_selected = signature["sort_label"] == "销量"
    old_card = page.locator(CARD_SELECTOR).filter(visible=True).first.element_handle() if before["cards"] else None
    try:
        if not already_selected:
            control = page.locator(SALES_TABS).filter(has_text=re.compile(r"^\s*销量\s*$")).filter(visible=True)

            def click_once():
                # 检查点的人工处理可能已选中销量，不再次点击或切换排序。
                while not entry_ready() or control.count() != 1:
                    guard.request_attention(page, "销量入口在点击前发生变化，请恢复原关键词第一页及唯一可用的销量入口；尚未点击", step="复核销量点击条件")
                if usable(sales=True):
                    return
                control.click(timeout=min(timeout_ms, 5000))

            guard.perform("点击淘宝销量排序", click_once, completed=lambda: usable(sales=True))
        guard.resume_check = lambda: usable(sales=True)
        deadline = time.monotonic() + timeout_ms / 1000
        manually_confirmed = False
        while True:
            paused = guard.paused_seconds
            guard.checkpoint("等待销量排序商品列表")
            deadline += guard.paused_seconds - paused
            state = read()
            fresh = already_selected or manually_confirmed
            if state:
                fresh = fresh or state[0]["document_started"] != before["document_started"] or _fingerprint(state[0]) != _fingerprint(before)
                if not fresh and old_card is not None:
                    try:
                        fresh = old_card.evaluate("e=>!e.isConnected")
                    except PlaywrightError:
                        pass
            if fresh and valid(state, sales=True):
                guard.pause_between("search")
                if usable(sales=True):
                    guard.checkpoint("销量排序已确认")
                    final = read()
                    if valid(final, sales=True):
                        print("销量排序已确认，接下来读取第一页已加载商品。", flush=True)
                        return final[2]
            if time.monotonic() >= deadline:
                guard.request_attention(page, "尚未确认销量已选中且商品列表更新，请人工核对销量排序和第一页后回复继续；不会再次点击销量或搜索", step="复核销量排序结果")
                manually_confirmed = True
                deadline = time.monotonic() + timeout_ms / 1000
            paused = guard.paused_seconds
            guard.wait(250)
            deadline += guard.paused_seconds - paused
    finally:
        if old_card is not None:
            try:
                old_card.dispose()
            except PlaywrightError:
                pass
