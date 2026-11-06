"""通过已登录的生意参谋搜索排行页面读取排行快照。"""
from datetime import date, timedelta
import re
from time import monotonic

from ecommerce.platforms.qianniu.collection_pacing import pause
from ecommerce.platforms.qianniu.verification import checkpoint, page_step, qianniu_operation

URL = 'https://sycm.taobao.com/mc/free/search_rank'
HEADER = '.item-cate .common-picker-header:visible'
DATE = '.oui-date-picker-current-date:visible'
PAGER = '.ant-pagination:visible'
PERIOD_DAYS = {'day': 1, '7d': 7, '30d': 30}
METRICS = {'搜索人气': 'popularity_raw', '点击率': 'click_rate_raw', '支付转化率': 'payment_conversion_raw'}


class RankingError(ValueError):
    """已知的排行页面读取失败。"""


def _until(page, read, message):
    deadline = monotonic() + 18
    while monotonic() < deadline:
        checkpoint(message)
        if result := page_step(read, description=message):
            return result
        page.wait_for_timeout(150)
    raise RankingError(message)


def _only(locator):
    return locator.count() == 1 and locator.is_visible()


def _path(text):
    return ' > '.join(part.strip() for part in (text or '').split('>'))


def _date_range(text, period):
    values = [date.fromisoformat(item) for item in re.findall(r'\d{4}-\d{2}-\d{2}', text)]
    if period == 'day' and len(values) == 1:
        return values[0], values[0]
    if len(values) == 2 and values[1] - values[0] == timedelta(days=PERIOD_DAYS[period] - 1):
        return tuple(values)
    return None


def _category(page):
    header = page.locator(HEADER)
    return _path(header.get_attribute('title', timeout=1000)) if _only(header) else ''


def _period_button(page, label):
    return page.locator('.oui-date-picker-particle-button').get_by_role('button', name=label, exact=True)


def _dates(page, period, label):
    button, date_label = _period_button(page, label), page.locator(DATE)
    if (_only(button) and 'ant-btn-primary' in (button.get_attribute('class') or '').split()
            and _only(date_label)):
        return _date_range(date_label.inner_text(), period)
    return None


def _radio(page, name):
    return page.get_by_role('radio', name=name, exact=True)


@qianniu_operation
def prepare(page, requested_path, period, label):
    """验证已知搜索排行入口、完整类目路径和统计周期。"""
    page_step(lambda: page.goto(URL, wait_until='domcontentloaded'), description='打开生意参谋搜索排行')
    header = page.locator(HEADER)
    _until(page, lambda: _only(header), '未找到生意参谋类目选择器，请核对账号权限')
    pause(page, 'page')
    changing_category = _category(page) != requested_path
    old = _table_state(page) if changing_category or not _dates(page, period, label) else None
    if changing_category:
        menu = page.locator('.item-cate .common-picker-menu:visible')
        if not _only(menu):
            page_step(header.click, completed=lambda: _only(menu), description='展开统计类目')
        field = menu.locator('input[placeholder="请输入关键字"]')
        _until(page, lambda: _only(field), '未找到类目搜索框')
        name = requested_path.split(' > ')[-1]
        pause(page, 'before_search')
        page_step(lambda: field.fill(name), completed=lambda: field.input_value() == name)
        item = menu.locator('.oui-typeahead-dropdown-item').filter(has_text=re.compile(r'^\s*' + re.escape(name) + r'\s*$'))
        _until(page, lambda: _only(item), '当前账号未找到唯一同名统计类目，请核对生意参谋完整路径')
        pause(page, 'search')
        page_step(item.click, completed=lambda: not _only(menu), description='选择统计类目')
        _until(page, lambda: _category(page) == requested_path,
               '生意参谋实际类目与请求路径不同，已停止读取')
        pause(page, 'page')
    control = _period_button(page, label)
    _until(page, lambda: _only(control), '未找到统计周期按钮')
    changing_period = not _dates(page, period, label)
    if changing_period:
        page_step(control.click, completed=lambda: bool(_dates(page, period, label)), description='切换统计周期')
    dates = _until(page, lambda: _dates(page, period, label), '无法确认统计周期对应的实际日期')
    if old and old['rows']:
        def refreshed():
            current = _table_state(page)
            return current and not current['loading'] and (current['empty'] or
                (current['rows'] and current['rows'] != old['rows']))
        _until(page, refreshed, '切换统计范围后未能确认排行数据已刷新')
    if changing_period:
        pause(page, 'page')
    if _category(page) != requested_path or _dates(page, period, label) != dates:
        raise RankingError('等待后统计类目或日期已改变')
    platform_id = header.get_attribute('data-category-id') or header.get_attribute('data-id')
    return requested_path, platform_id, *dates


def _table(page):
    return page.locator('table:visible').filter(has=page.get_by_role('columnheader', name='排名', exact=True))


def _table_state(page):
    table = _table(page)
    if not _only(table):
        return None
    return table.evaluate("""table => {
      const container = table.closest('.ant-table-wrapper') || table.parentElement;
      const visible = element => element.getClientRects().length > 0;
      return {
        loading: [...container.querySelectorAll('.ant-spin-spinning')].some(visible),
        empty: [...container.querySelectorAll('.ant-table-placeholder,.ant-empty')].some(
          node => visible(node) && /暂无数据|无数据/.test(node.innerText)),
        headers: [...table.querySelectorAll('thead th')].map(cell => cell.innerText.trim()),
        rows: [...table.querySelectorAll('tbody tr[data-row-key]')].map(row =>
          [...row.cells].map(cell => ({text:cell.innerText.trim(),medal:!!cell.querySelector('.rank-img')})))
      };
    }""")


def parse_rows(headers, rows, word_label, page_no):
    """保留指标原值，校验实际名次，首屏奖牌按位置读为前三名。"""
    if headers[:2] != ['排名', word_label] or not METRICS.keys() <= set(headers) or len(set(headers)) != len(headers):
        raise RankingError('搜索排行表头与词类型不一致')
    result = []
    for index, row in enumerate(rows, 1):
        if len(row) != len(headers):
            raise RankingError('排行数据列数与表头不一致')
        rank = row[0]['text']
        if page_no == 1 and index <= 3 and row[0]['medal']:
            rank = str(index)
        if not re.fullmatch(r'[1-9]\d*', rank) or not row[1]['text']:
            raise RankingError('有一行缺少有效的排名或词原文')
        item = {'rank': int(rank), 'keyword': row[1]['text'], 'extra_metrics': {}}
        for heading, cell in zip(headers[2:], row[2:]):
            if heading in METRICS:
                item[METRICS[heading]] = cell['text']
            elif heading and heading != '操作':
                item['extra_metrics'][heading] = cell['text']
        result.append(item)
    return result


def _page_no(page):
    pager = page.locator(f'{PAGER} .ant-pagination-item-active')
    if not _only(pager):
        return None
    raw = pager.get_attribute('title') or ''
    return int(raw) if raw.isdecimal() else None


def _page_data(page, scope, word_label, number, *, previous=None):
    path, period, period_label, dates = scope
    signature = None
    seen_at = 0

    def ready():
        nonlocal signature, seen_at
        if (_category(page) != path or _dates(page, period, period_label) != dates or
                not _radio(page, word_label).is_checked() or not _radio(page, '搜索人气').is_checked()):
            return None
        state = _table_state(page)
        if not state or state['loading'] or state['headers'][:2] != ['排名', word_label]:
            return None
        pager = _page_no(page)
        if state['rows'] and (pager != number or state['rows'] == previous):
            return None
        if not state['rows'] and (not state['empty'] or number != 1 or pager not in (None, 1)):
            return None
        if signature != state:
            signature, seen_at = state, monotonic()
            return None
        return state if monotonic() - seen_at >= .3 else None

    return _until(page, ready, f'无法确认{word_label}第 {number} 页已加载，或统计范围已经改变')


@qianniu_operation
def collect_type(page, scope, word_label, requested, save):
    """逐页保存一个词类型，结束时复核首页的词与排名。"""
    number = 1
    try:
        old = _table_state(page)
        radio = _radio(page, word_label)
        _until(page, lambda: _only(radio), f'页面没有可选的{word_label}')
        changed_type = not radio.is_checked()
        if changed_type:
            page_step(radio.click, completed=radio.is_checked, description=f'切换{word_label}')
            def type_ready():
                state = _table_state(page)
                return state and not state['loading'] and state['headers'][:2] == ['排名', word_label]
            _until(page, type_ready, f'{word_label}榜单尚未就绪')
            pause(page, 'keyword')
        ranking = _radio(page, '搜索人气')
        _until(page, lambda: _only(ranking) and ranking.is_enabled(), f'{word_label}没有可选的搜索人气榜')
        changed_sort = not ranking.is_checked()
        if changed_sort:
            page_step(ranking.click, completed=ranking.is_checked, description='切换搜索人气榜')
        needs_first = _page_no(page) not in (None, 1)
        previous = old['rows'] if old and (changed_sort or needs_first) else None
        if changed_sort and needs_first:
            pause(page, 'page')
        if needs_first:
            _go_first(page)
        state = _page_data(page, scope, word_label, 1, previous=previous)
        if needs_first or changed_sort:
            pause(page, 'page')
            state = _page_data(page, scope, word_label, 1, previous=previous)
        if not state['rows']:
            return 'empty', None, None
        first = None
        seen = set()
        while True:
            entries = parse_rows(state['headers'], state['rows'], word_label, number)
            order = [(entry['rank'], entry['keyword']) for entry in entries]
            # 平台在同一页可能显示 35、34 或跳过 40。保留原名次和页面顺序。
            if seen.intersection(order):
                return 'changed', number, '同一排行词及名次在不同页重复出现，建议重采此词类型'
            if first is None:
                first = order
            save(number, entries)
            seen.update(order)
            next_page = page.locator(f'{PAGER} .ant-pagination-next')
            if not _only(next_page):
                raise RankingError('未找到排行的下一页控件')
            if next_page.get_attribute('aria-disabled') == 'true':
                reason = 'end'
                break
            if number == requested:
                reason = 'requested'
                break
            number += 1
            page_step(next_page.click, completed=lambda: _page_no(page) == number, description=f'翻到第 {number} 页')
            previous = state['rows']
            _page_data(page, scope, word_label, number, previous=previous)
            pause(page, 'page')
            state = _page_data(page, scope, word_label, number, previous=previous)
        previous = state['rows'] if number > 1 else None
        if number > 1:
            _go_first(page)
        number = 1
        check = _page_data(page, scope, word_label, 1, previous=previous)
        if previous is not None:
            pause(page, 'page')
            check = _page_data(page, scope, word_label, 1, previous=previous)
        entries = parse_rows(check['headers'], check['rows'], word_label, 1)
        if [(entry['rank'], entry['keyword']) for entry in entries] != first:
            return 'changed', 1, '采集期间第一页排名或词发生变化，建议重采此词类型'
        return reason, None, None
    except RankingError as error:
        return 'error', number, str(error)


def _go_first(page):
    first = page.locator(f'{PAGER} .ant-pagination-item[title="1"]')
    _until(page, lambda: _only(first), '未找到返回第一页的控件')
    page_step(first.click, completed=lambda: _page_no(page) == 1, description='返回第一页核对')
