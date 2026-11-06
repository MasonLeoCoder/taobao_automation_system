"""在已登录的千牛发品页逐列只读采集。"""
from time import monotonic

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .product import SEARCH_INPUT_SELECTOR, click_start_button
from .verification import checkpoint, expect, page_step, qianniu_operation

SEARCH = 'input[placeholder="名称/拼音首字母"]:visible'

_READ_COLUMN = """(input) => {
    const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const cssPath = element => {
        const parts = [];
        for (let e = element; e && e !== document.body; e = e.parentElement)
            parts.unshift(':nth-child(' + (Array.from(e.parentElement.children).indexOf(e) + 1) + ')');
        return 'body > ' + parts.join(' > ');
    };
    const panel = input.closest('.category-list');
    const list = panel?.querySelector(':scope > .list-frame');
    if (!list) return null;
    const rows = [...list.querySelectorAll('li.category-item,.group-title')].filter(visible).map(el => {
        const group = el.classList.contains('group-title');
        const icon = el.querySelector('.icon-wrap .leaf-icon');
        return {name:(el.getAttribute('title') || el.innerText).trim(), group,
            expanded:group && [...el.parentElement.querySelectorAll('li.category-item')].some(visible),
            branch:Boolean(icon), selected:el.classList.contains('selected'), selector:cssPath(el),
            platformId:el.getAttribute('data-cid') || el.getAttribute('data-category-id') || null};
    });
    const loading = panel.closest('.next-loading') || panel;
    return {rows, scroller:cssPath(list), loading:
        [...loading.querySelectorAll('[aria-busy=true],[role=progressbar],.next-loading-tip,.next-loading-indicator')].some(visible)};
}"""


class CategoryPageError(ValueError):
    """页面未能确认类目列表，不能把结果解释为空分支。"""


def _state(page, column):
    fields = page.locator(SEARCH)
    if fields.count() <= column:
        return None
    return fields.nth(column).evaluate(_READ_COLUMN)


def _ready(page, column, predicate=lambda state: True):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        checkpoint('等待千牛类目栏目更新')
        state = page_step(lambda: _state(page, column))
        if state and not state['loading'] and state['rows'] and predicate(state):
            return state
        page.wait_for_timeout(120)
    raise CategoryPageError(f'无法确认第 {column + 1} 列已加载')


def _scroll(page, state, top=None):
    return page_step(lambda: page.locator(state['scroller']).evaluate("""(element, top) => {
        const before = element.scrollTop;
        element.scrollTop = top === null ? before + Math.max(element.clientHeight * .75, 1) : top;
        return {before, after:element.scrollTop, height:element.scrollHeight, size:element.clientHeight};
    }""", top))


def _read_all(page, column, *, expand_groups=False):
    state = _ready(page, column)
    _scroll(page, state, 0)
    rows, groups, stable_bottom = {}, set(), None
    for _ in range(500):
        state = _ready(page, column)
        for row in state['rows']:
            if row['group']:
                if expand_groups and not row['expanded'] and row['name'] not in groups:
                    page_step(lambda: page.locator(row['selector']).click(),
                              completed=lambda: any(item['name'] == row['name'] and item['expanded']
                                                    for item in (_state(page, column) or {'rows': []})['rows']))
                    _ready(page, column, lambda state: any(item['name'] == row['name'] and item['expanded']
                                                           for item in state['rows']))
                    groups.add(row['name'])
                    stable_bottom = None
                    break
                continue
            name = row['name']
            if ' > ' in name or len(name) > 249:
                raise CategoryPageError('类目名称无法安全写入完整路径')
            if name in rows and rows[name]['platformId'] != row['platformId']:
                raise CategoryPageError('同列出现同名类目，需先核对千牛 ID')
            rows[name] = row
        else:
            movement = _scroll(page, state)
            if movement['after'] == movement['before'] and movement['after'] + movement['size'] >= movement['height'] - 1:
                bottom = (movement['height'], tuple(rows))
                if bottom == stable_bottom and rows:
                    return rows
                stable_bottom = bottom
            else:
                stable_bottom = None
        page.wait_for_timeout(150)
    raise CategoryPageError(f'第 {column + 1} 列未能确认已读到底')


def _pick(page, column, name, *, branch):
    _scroll(page, _ready(page, column), 0)
    for _ in range(500):
        state = _ready(page, column)
        rows = [row for row in state['rows'] if not row['group'] and row['name'] == name]
        if len(rows) == 1:
            row = rows[0]
            if row['branch'] != branch:
                raise CategoryPageError('类目下级状态在读取期间变化')
            previous = _state(page, column + 1)

            def selected():
                current = _state(page, column)
                return current and any(item['name'] == name and item['selected'] for item in current['rows'])

            if not row['selected']:
                page_step(lambda: page.locator(row['selector']).click(), completed=selected)
            if branch:
                # 切换父级后必须看到新的子列，不能沿用前一个父级的旧内容。
                def matches(children):
                    return selected() and (row['selected'] or previous is None or
                        [(item['name'], item['platformId']) for item in children['rows']] !=
                        [(item['name'], item['platformId']) for item in previous['rows']])
                _ready(page, column + 1, matches)
            else:
                deadline = monotonic() + 10
                while monotonic() < deadline:
                    checkpoint('核对末级类目')
                    if selected() and page.locator(SEARCH).count() == column + 1:
                        return
                    page.wait_for_timeout(120)
                raise CategoryPageError('无法确认所选类目为末级')
            return
        if len(rows) > 1:
            break
        movement = _scroll(page, state)
        if movement['after'] == movement['before']:
            break
        page.wait_for_timeout(150)
    raise CategoryPageError(f'无法唯一找到类目「{name}」')


@qianniu_operation
def collect_publish_categories(page, scopes, *, on_progress=None):
    """返回已观察节点、完整父列和失败分支。空范围表示全量。"""
    search = page.locator(SEARCH).first
    if not search.is_visible():
        if not page.locator(SEARCH_INPUT_SELECTOR).is_visible():
            click_start_button(page)
        button = page.get_by_text('选择类目', exact=True)
        expect(button).to_be_visible(timeout=15_000)
        page_step(button.click, completed=search.is_visible)
        expect(search).to_be_visible(timeout=15_000)
    observed, complete, failed = {}, set(), []
    targets = [tuple(path.split(' > ')) for path in scopes]

    def visit(column, parent):
        try:
            rows = _read_all(page, column, expand_groups=not column)
        except (CategoryPageError, PlaywrightTimeout) as error:
            failed.append({'path': ' > '.join(parent), 'reason': str(error).splitlines()[0]})
            return
        all_children = not targets or any(parent[:len(target)] == target for target in targets)
        if not all_children:
            wanted = {target[column] for target in targets if target[:column] == parent and len(target) > column}
            for missing in sorted(wanted - rows.keys()):
                failed.append({'path': ' > '.join((*parent, missing)),
                               'reason': '未找到指定分支，请从上一级重新采集'})
            rows = {name: row for name, row in rows.items() if name in wanted}
        if all_children:
            complete.add(parent)
        for name, row in rows.items():
            parts = (*parent, name)
            try:
                if len(' > '.join(parts)) > 750 or column >= 12:
                    raise CategoryPageError('类目路径超过当前采集范围，请复核页面结构')
                if on_progress and column < 2:
                    on_progress(' > '.join(parts))
                if row['branch'] or column < 2:
                    _pick(page, column, name, branch=row['branch'])
                observed[parts] = (row['platformId'], not row['branch'])
                if row['branch']:
                    visit(column + 1, parts)
            except (CategoryPageError, PlaywrightTimeout) as error:
                # 当前列的这个条目存在，但状态不明。不能将它或兄弟类目核销。
                complete.discard(parent)
                failed.append({'path': ' > '.join(parts), 'reason': str(error).splitlines()[0]})

    visit(0, ())
    return observed, complete, failed
