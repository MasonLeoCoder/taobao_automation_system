"""按真实分页控件单次点击下一页，确认页码、排序和列表更新后再返回。"""
import time

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.tasks.events import emit_event
from ecommerce.platforms.taobao.cards import CARD_SELECTOR, SNAPSHOT, page_signature
from ecommerce.platforms.taobao.search import inspect_search
from ecommerce.platforms.taobao.verification import active_guard


def validate_pages(value):
    """验证请求页数；参数：从第一页起的页数；返回：原整数。"""
    if type(value) is not int or value < 1:
        raise ValueError("--page须为正整数，默认只采集第一页")
    return value


def inspect_pagination(raw, page_number):
    """核对已适配分页证据；参数：快照、当前页；返回：按钮选择器或末页证据。"""
    state = raw.get('pagination', {})
    if state.get('group_count') != 1 or state.get('variant') not in {'normal', 'simple'}:
        raise ValueError('无法唯一确认已适配的分页栏')
    totals = state.get('total_pages', [])
    if len(totals) > 1 or (totals and (not str(totals[0]).isdigit() or int(totals[0]) < page_number)):
        raise ValueError('分页总页数不明确或与当前页矛盾')
    total = int(totals[0]) if totals else None
    count, disabled = state.get('next_count'), state.get('next_disabled')
    if total == page_number and (count == 0 or (count == 1 and disabled is True)):
        return {'last_page': True, 'page_number': page_number, 'total_pages': total,
                'basis': 'current_equals_total_and_no_enabled_next'}
    if count != 1 or disabled is not False or total == page_number:
        raise ValueError('下一页入口缺失或禁用，但尚未确认已到末页')
    return {'last_page': False, 'total_pages': total,
            'selector': '.next-pagination.next-' + state['variant'] + ' button.next-next'}


def _identity(raw):
    """提取列表变化证据；参数：快照；返回：有序商品特征。"""
    return [(card.get('dom_item_id'), card.get('title'), card.get('price_number_text')) for card in raw['cards']]


def advance_next_page(result, page_number, timeout_ms):
    """单次翻页并复核；参数：搜索页、已保存页码、超时；返回：新搜索页及证据，末页时新页为空。"""
    guard = active_guard()
    if guard is None:
        raise RuntimeError('翻页必须在淘宝守卫和统一任务中执行')
    page, keyword = result.page, result.keyword
    target = page_number + 1

    def read(number):
        """只读检查目标页；参数：目标页码；返回：快照、签名和搜索状态或空。"""
        try:
            raw = page.evaluate(SNAPSHOT)
            signature = page_signature(raw, keyword, expected_page=number)
            confirmed = inspect_search(page, keyword)
            if raw['list_busy'] or signature['sort_label'] != '销量' or confirmed is None:
                return None
            return raw, signature, confirmed
        except (PlaywrightError, ValueError):
            return None

    guard.resume_check = lambda: read(page_number) is not None
    while True:
        guard.checkpoint(f'第{page_number}页已保存，检查下一页')
        initial = read(page_number)
        try:
            if initial is None:
                raise ValueError(f'当前关键词、销量排序或第{page_number}页尚未确认')
            state = inspect_pagination(initial[0], page_number)
            break
        except ValueError as error:
            guard.request_attention(page, str(error) + '；尚未点击下一页', step='检查下一页入口')
    if state['last_page']:
        emit_event('pagination_end_confirmed', '已确认搜索结果末页', details=state)
        return None, state

    before = initial[0]
    old_card = page.locator(CARD_SELECTOR).filter(visible=True).first.element_handle() if before['cards'] else None
    manual_confirmed = False

    def basis(current):
        """确认列表更新；参数：新快照；返回：证据代码或空。"""
        if current['document_started'] != before['document_started']:
            return 'document_navigation'
        if _identity(current) != _identity(before):
            return 'list_signature_changed'
        if old_card is not None:
            try:
                if old_card.evaluate('e=>!e.isConnected'):
                    return 'old_card_detached'
            except PlaywrightError:
                pass
        return 'manual_confirmation' if manual_confirmed else None

    def completed():
        """只读核对动作结果；参数：无；返回：目标页及列表是否已确认。"""
        current = read(target)
        return bool(current and basis(current[0]))

    try:
        guard.resume_check = lambda: read(page_number) is not None or read(target) is not None
        control = page.locator(state['selector']).filter(visible=True)

        def click_once():
            """检查暂停期间变化后发出一次点击；参数：无；返回：无。"""
            while True:
                current = read(page_number)
                if current:
                    try:
                        check = inspect_pagination(current[0], page_number)
                        valid = not check['last_page'] and check['selector'] == state['selector'] and control.count() == 1 and control.is_enabled()
                    except ValueError:
                        valid = False
                    if valid:
                        break
                guard.request_attention(page, f'点击前条件变化，请恢复第{page_number}页及原搜索排序；尚未点击', step='复核下一页点击条件')
            emit_event('pagination_click', '点击下一页一次', details={'from_page': page_number, 'target_page': target})
            control.click(timeout=min(timeout_ms, 5000))

        guard.perform(f'从第{page_number}页点击下一页', click_once, completed=completed)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            paused = guard.paused_seconds
            guard.checkpoint(f'等待第{target}页列表更新')
            deadline += guard.paused_seconds - paused
            current = read(target)
            if current and basis(current[0]):
                guard.pause_between('page')
                current = read(target)
                if current and basis(current[0]):
                    evidence = {'from_page': page_number, 'page_number': target, 'click_count': 1,
                                'list_update_basis': basis(current[0]), 'total_pages': state['total_pages']}
                    guard.update_progress(keyword=keyword, page_number=target, collected=0)
                    guard.resume_check = lambda: read(target) is not None
                    emit_event('pagination_verified', '目标页码、关键词、销量及列表更新已确认', details=evidence)
                    return current[2], evidence
            if time.monotonic() >= deadline:
                guard.request_attention(page, f'下一页点击已发出，尚未确认第{target}页及列表更新；请检查后继续，不会重复点击', step='复核下一页结果')
                manual_confirmed = True
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
