"""保留搜索页并打开已确认商品的详情页，统一经过淘宝验证守卫。"""
from playwright.sync_api import Error as PlaywrightError

from ecommerce.platforms.taobao.cards import PAGE_METADATA, page_signature
from ecommerce.platforms.taobao.detail import matches_detail_url, open_detail, resolve_detail_input
from ecommerce.platforms.taobao.search import inspect_search
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import ModuleBlocked


def matches_search_page(result, page_number):
    """只读核对返回现场；参数：搜索页、实际页码；返回：关键词、页码和销量是否一致。"""
    try:
        raw = result.page.evaluate(PAGE_METADATA)
        signature = page_signature(raw, result.keyword, expected_page=page_number)
        return (signature['sort_label'] == '销量' and not raw['list_busy'] and
                inspect_search(result.page, result.keyword) is not None)
    except (PlaywrightError, ValueError):
        return False


def confirm_search_page(result, page_number):
    """等待搜索页恢复已采条件；参数：搜索结果、页码；返回：无，不重搜或重新排序。"""
    guard = active_guard()
    guard.resume_check = lambda: matches_search_page(result, page_number)
    while True:
        guard.checkpoint('核对列表与详情之间的搜索页现场')
        if matches_search_page(result, page_number):
            return
        guard.request_attention(result.page, '搜索页关键词、销量排序或页码发生变化，请恢复本页后继续；不会自动重搜',
                                step='复核保留的搜索页')


def open_list_detail(context, url, product_id, timeout_ms):
    """登记唯一预期标签并打开标准链接；参数：会话、地址、商品号、超时；返回：详情页。"""
    target = resolve_detail_input(url)
    if target.item_id != product_id:
        raise ValueError('列表详情链接与已采集商品编号不一致')
    guard = active_guard()
    guard.checkpoint('准备从列表打开详情')
    with guard.expect_new_page(lambda page: matches_detail_url(page, target.item_id), timeout_ms):
        page = context.new_page()
        try:
            with guard.collection_module('detail_open',page,timeout_ms):
                return open_detail(page, target.url, timeout_ms)
        except ModuleBlocked:
            if not page.is_closed():page.close()
            raise


def return_to_search(result, detail, page_number):
    """完成入库后关闭本次详情并复核列表；参数：搜索页、详情页、页码；返回：无。"""
    guard = active_guard()
    confirm_search_page(result, page_number)
    guard.perform('关闭已完成的商品详情标签', detail.page.close)
    result.page.bring_to_front()
    confirm_search_page(result, page_number)
