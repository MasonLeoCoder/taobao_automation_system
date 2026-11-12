"""千牛自定义销售规格创建、SKU明细填写及回读核对。"""
import json
import re
from decimal import Decimal, InvalidOperation
from collections import Counter
from logging import getLogger
from playwright.sync_api import Error as PlaywrightError, Page, expect as playwright_expect
from ecommerce.shared.exceptions import NeedsAttention
from .layered_sku_table import FIXED_TABLE, READ_LAYERED_TABLE
from .verification import checkpoint, qianniu_operation, expect, page_step

SKU_GROUP = "#sell-field-sku"
DRAWER = ".sku-decouple-drawer:visible"
CREATED_NAMES = ".sku-unique-title-textarea textarea[data-real='true']"
WAIT_MS = 30000
SKU_ROWS = "tr.sku-table-row"
DETAIL_CELLS = {"价格": "td[id$='-skuPrice']", "数量": "td[id$='-skuStock']", "商家编码": "td[id$='-skuOuterId']"}
CATEGORY_CELL = "td[id$='-skuQuality']"
CATEGORY_POPUP = ".sell-o-select-popup-overlay:visible"
LAYERED_DIMENSIONS = ("颜色分类", "适用尺码")
LAYERED_PLACEHOLDERS = (re.compile(r"^主色\s*[（(]\s*必选\s*[）)]$"), re.compile(r"^请输入$"))


def _values(fields) -> list[str]:
    """读取全部已渲染规格输入；参数：定位器；返回：原值及页面顺序。"""
    return fields.evaluate_all("els => els.map(e => e.value)")


def _check_drawer_errors(drawer) -> None:
    """检查规格平台错误；参数：设置面板；返回：有错误时抛出人工处理异常。"""
    errors = drawer.locator(".sell-o-message[class*='errorMessage-']:visible, .sell-component-info-wrapper-msg-content.error:visible")
    messages = [text.strip() for text in errors.all_text_contents() if text.strip()]
    if messages:
        raise NeedsAttention("销售规格未通过平台校验：" + "；".join(messages))


def _verify_created(group, names: list[str]) -> None:
    """核对创建结果；参数：销售规格区域、预期名称；返回：数量及完整顺序一致后返回。"""
    fields = group.locator(CREATED_NAMES)
    expect(fields).to_have_count(len(names), timeout=WAIT_MS)
    for index, name in enumerate(names):
        expect(fields.nth(index)).to_have_value(name, timeout=WAIT_MS)


@qianniu_operation
def create_sales_specifications(page: Page, names: list[str]) -> list[str]:
    """创建销售规格；参数：商品页、Excel按行排列的规格名称；返回：已核对的规格列表。"""
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("销售规格需要至少一个非空文本名称")
    names = [name.strip() for name in names]
    if len(set(names)) != len(names):
        raise ValueError("销售规格名称不能重复")
    logger = getLogger(__name__)
    logger.info("准备填写销售规格：共%d项，Excel顺序=%r", len(names), names)
    group = page.locator(SKU_GROUP)
    expect(group).to_be_visible(timeout=WAIT_MS)
    existing = group.locator(CREATED_NAMES)
    drawer = page.locator(DRAWER)
    if group.locator("tr.sku-table-row").count():
        if drawer.count():
            raise NeedsAttention("页面已有规格且设置面板仍打开，请先核对当前编辑状态")
        if _values(existing) != names:
            raise NeedsAttention("页面已有销售规格与Excel数量或顺序不一致，已停止，未覆盖或追加")
        _verify_created(group, names)
        logger.info("销售规格已存在且数量、顺序一致：共%d项；未重复创建", len(names))
        return names
    if not drawer.count():
        page_step(lambda: group.get_by_role("button", name="创建规格", exact=False).click(timeout=WAIT_MS), completed=lambda: drawer.count() == 1)
    expect(drawer).to_have_count(1, timeout=WAIT_MS)
    custom = drawer.get_by_role("radio", name=re.compile(r"^(?:单层展示\s*[:：·・]?\s*)?自定义填写规格$"))
    if not custom.is_checked():
        # 防止切换模式时丢弃用户已填写的标准属性规格。
        standard_values = drawer.locator(".sale-props-value input").evaluate_all("els => els.map(e => e.value).filter(Boolean)")
        if standard_values:
            raise NeedsAttention("规格面板已有标准属性内容，请先核对，未切换模式")
        page_step(lambda: custom.check(timeout=WAIT_MS), completed=custom.is_checked)
    expect(custom).to_be_checked(timeout=WAIT_MS)
    fields = drawer.get_by_placeholder("输入规格", exact=True)
    expect(fields.first).to_be_visible(timeout=WAIT_MS)
    initial = _values(fields)
    if len(initial) > len(names) or any(value and value != names[index] for index, value in enumerate(initial)):
        raise NeedsAttention("规格面板已有不同内容或额外空行，请核对，未覆盖或删除")
    for index, name in enumerate(names):
        checkpoint(f"填写销售规格 {index + 1}/{len(names)}：{name}")
        if fields.count() == index:
            add = drawer.locator("button.add")
            expect(add).to_have_count(1, timeout=WAIT_MS)
            expect(add).to_be_enabled(timeout=WAIT_MS)
            page_step(lambda: add.click(timeout=WAIT_MS), completed=lambda: fields.count() == index + 1)
            expect(fields).to_have_count(index + 1, timeout=WAIT_MS)
        field = fields.nth(index)
        if field.input_value() != name:
            page_step(lambda: field.fill(name, timeout=WAIT_MS))
        page_step(lambda: field.press("Tab", timeout=WAIT_MS), completed=lambda: field.evaluate("e=>document.activeElement!==e"))
        expect(field).to_have_value(name, timeout=WAIT_MS)
        _check_drawer_errors(drawer)
        logger.info("销售规格第%d/%d项已填写并回读核对：%s", index + 1, len(names), name)
    expect(fields).to_have_count(len(names), timeout=WAIT_MS)
    if _values(fields) != names:
        raise NeedsAttention("确认创建前销售规格的数量或顺序不一致")
    expect(drawer.locator(".props-label")).to_have_text(f"商品规格({len(names)})", timeout=WAIT_MS)
    _check_drawer_errors(drawer)
    page_step(lambda: drawer.get_by_role("button", name="确认创建", exact=True).click(timeout=WAIT_MS), completed=lambda: drawer.count() == 0 and group.locator("tr.sku-table-row").count() == len(names))
    expect(drawer).to_have_count(0, timeout=WAIT_MS)
    _verify_created(group, names)
    logger.info("销售规格创建完成并核对数量、顺序：共%d项，实际规格=%r", len(names), names)
    return names


def layered_sku_rows(group, combinations, *, only=None):
    """按表头读取组合，兼容同色行合并。核对通过后才返回 Excel 规格到行的映射。"""
    rows = group.locator(SKU_ROWS)
    actual = rows.evaluate_all(READ_LAYERED_TABLE, list(LAYERED_DIMENSIONS))
    if actual.get('error'):
        raise NeedsAttention(actual['error'])
    entries = actual['entries']
    pairs = [tuple(item['pair']) for item in entries]
    expected = {}
    for name, values in combinations.items():
        pair = tuple(" ".join(value.split()) for value in values)
        if pair in expected:
            raise NeedsAttention("Excel 组合的显示文字无法唯一匹配：" + " × ".join(pair))
        expected[pair] = name
    unexpected = dict.fromkeys(pair for pair in pairs if pair not in expected)
    missing = [pair for pair in expected if pair not in pairs]
    duplicates = [pair for pair, count in Counter(pairs).items() if count > 1]
    if unexpected or missing or duplicates:
        issues = [label + "：" + "、".join(" × ".join(pair) for pair in values)
                  for label, values in (("多出", unexpected), ("缺少", missing), ("重复", duplicates)) if values]
        raise NeedsAttention("千牛分层 SKU 与 Excel 不一致。" + "；".join(issues))
    result = {}
    for item, pair in zip(entries, pairs):
        name = expected[pair]
        if only is not None and name != only:
            continue
        if not item['codeId']:
            result[name] = rows.nth(item['index'])
            continue
        code_cell = group.page.locator(f'td[id={json.dumps(item["codeId"], ensure_ascii=False)}] input')
        row = rows.filter(has=code_cell)
        for label in ("价格", "数量"):
            row = row.filter(has=group.page.locator(f'{DETAIL_CELLS[label]} input'))
        if not actual['fixed']:
            fixed_rows = ', '.join(f'{selector.strip()} tr' for selector in FIXED_TABLE.split(','))
            row = row.and_(group.page.locator(f'tr:not({fixed_rows})'))
        if row.count() != 1:
            raise NeedsAttention(f"SKU「{name}」的商家编码输入行无法唯一确认")
        result[name] = row
    return result


def _dimension_heading(label):
    return re.compile(rf"^{label}\s*[（(]\s*\d+\s*[）)]$")


def _layered_controls(drawer, index):
    """从属性计数标题定位独立区块，并按输入提示和添加按钮定位控件。"""
    label = LAYERED_DIMENSIONS[index]
    heading = drawer.get_by_text(_dimension_heading(label))
    if heading.count() != 1:
        raise NeedsAttention(f"无法唯一找到分层规格的「{label}」区块")
    other = _dimension_heading(LAYERED_DIMENSIONS[1 - index])
    section = heading
    visible_fields = None
    field_count = 0
    for _ in range(8):
        section = section.locator("..")
        if section.get_by_text(other).count():
            break
        fields = section.get_by_placeholder(LAYERED_PLACEHOLDERS[index]).and_(section.locator("input:visible"))
        add = section.get_by_role("button", name=re.compile(r"^[+＋]$|^添加(?:颜色|尺码|规格)?$")).or_(
            section.locator('button[class*="add" i]:visible, button:has(svg[data-icon="plus"]):visible'))
        count = fields.count()
        if count:
            if add.count() == 1:
                return fields, add
            if count > field_count:
                visible_fields, field_count = (fields, add), count
    if visible_fields is not None:
        return visible_fields
    field_name = "主色（必选）" if index == 0 else "请输入"
    raise NeedsAttention(f"无法在「{label}」区块唯一定位「{field_name}」输入框和添加按钮")


def _fill_layered_dimensions(drawer, combinations):
    """逐项核对已有值，填写空行并在每次添加后验证结果。"""
    for dimension, label in enumerate(LAYERED_DIMENSIONS):
        fields, add = _layered_controls(drawer, dimension)
        values = list(dict.fromkeys(pair[dimension] for pair in combinations.values()))
        existing = _values(fields)
        if len(existing) > len(values):
            raise NeedsAttention(f"「{label}」多出第 {len(values) + 1} 项，请核对千牛已有内容")
        for index, actual in enumerate(existing):
            if actual and actual != values[index]:
                raise NeedsAttention(f"「{label}」第 {index + 1} 项应为「{values[index]}」，千牛显示「{actual}」")
        for index, value in enumerate(values):
            item = f"「{label}」第 {index + 1} 项「{value}」"
            checkpoint(f"填写分层规格 {item}")
            operation = "添加输入行"
            try:
                if fields.count() == index:
                    if add.count() != 1 or not add.is_enabled():
                        raise NeedsAttention("无法使用唯一的添加按钮，请先核对上一项")
                    add.click(timeout=WAIT_MS)
                    playwright_expect(fields).to_have_count(index + 1, timeout=WAIT_MS)
                operation = "填写并回读"
                field = fields.nth(index)
                current = field.input_value(timeout=WAIT_MS)
                if current and current != value:
                    raise NeedsAttention(f"千牛显示「{current}」")
                if current != value:
                    field.fill(value, timeout=WAIT_MS)
                    field.press("Tab", timeout=WAIT_MS)
                playwright_expect(field).to_have_value(value, timeout=WAIT_MS)
                _check_drawer_errors(drawer)
            except NeedsAttention as error:
                raise NeedsAttention(f"{item}无法确认。{error}") from error
            except (PlaywrightError, AssertionError) as error:
                raise NeedsAttention(f"{item}在{operation}时未获得预期结果，请核对该项") from error
        if _values(fields) != values:
            raise NeedsAttention(f"「{label}」当前值与 Excel 不一致，请按 Excel 核对")


@qianniu_operation
def create_layered_specifications(page, combinations, *, on_manual):
    """自动填写分层规格，人工反馈后复查本步骤和实际组合。"""
    group = page.locator(SKU_GROUP)
    drawer = page.locator(DRAWER)
    expect(group).to_be_visible(timeout=WAIT_MS)
    rows = group.locator(SKU_ROWS)
    creation_requested = False
    while True:
        checkpoint("核对分层销售规格")
        if page.is_closed():
            raise NeedsAttention("千牛发品页已关闭，分层规格填写已结束")
        try:
            if not drawer.count() and rows.count():
                layered_sku_rows(group, combinations)
                return
            if creation_requested:
                raise NeedsAttention("千牛尚未显示创建后的分层规格表，请核对创建结果")
            if not drawer.count():
                page_step(lambda: group.get_by_role("button", name="创建规格", exact=False).click(timeout=WAIT_MS),
                          completed=lambda: drawer.count() == 1)
            if drawer.count() != 1:
                raise NeedsAttention("无法唯一找到销售规格设置面板")
            layered = drawer.get_by_role("radio", name=re.compile(r"分层展示|选择标准属性构建规格"))
            if layered.count() != 1:
                raise NeedsAttention("无法唯一找到「分层展示」选项")
            if not layered.is_checked():
                if any(_values(drawer.get_by_placeholder("输入规格", exact=True))):
                    raise NeedsAttention("千牛已有单层规格内容，请先核对并切换到「分层展示」")
                page_step(lambda: layered.check(timeout=WAIT_MS), completed=layered.is_checked)
            _fill_layered_dimensions(drawer, combinations)
            confirm = drawer.get_by_role("button", name="确认创建", exact=True)
            if confirm.count() != 1 or not confirm.is_enabled():
                raise NeedsAttention("颜色和尺码已经核对，「确认创建」按钮仍不可用")
            creation_requested = True
            try:
                confirm.click(timeout=WAIT_MS)
                playwright_expect(drawer).to_have_count(0, timeout=WAIT_MS)
                rows.first.wait_for(state="visible", timeout=WAIT_MS)
            except (PlaywrightError, AssertionError) as error:
                if drawer.count() or not rows.count():
                    raise NeedsAttention("已请求创建分层规格，请核对千牛实际创建结果") from error
        except NeedsAttention as error:
            getLogger(__name__).warning("分层规格待核对：%s", error)
            on_manual(str(error))


def _numeric_pattern(value: str) -> re.Pattern:
    """允许页面保留末尾小数零；参数：规范数值文本；返回：数值等价的回读匹配式。"""
    whole, _, fraction = value.partition(".")
    decimal = rf"\.{re.escape(fraction)}0*" if fraction else r"(?:\.0+)?"
    return re.compile(rf"^0*{re.escape(whole)}{decimal}$")


def _fill_sku_field(field, value: str, numeric: bool) -> None:
    """填写SKU单元格；参数：输入框、原值、是否数值；返回：失焦并完整核对后返回。"""
    current = field.input_value()
    same = current == value
    if numeric:
        try:
            same = Decimal(current) == Decimal(value)
        except InvalidOperation:
            same = False
    if not same:
        page_step(lambda: field.fill(value, timeout=WAIT_MS))
    page_step(lambda: field.press("Tab", timeout=WAIT_MS), completed=lambda: field.evaluate("e=>document.activeElement!==e"))
    expect(field).to_have_value(_numeric_pattern(value) if numeric else value, timeout=WAIT_MS)


def _select_sku_category(page: Page, row, category: str, name: str) -> None:
    """选择指定SKU分类；参数：页面、规格行、分类、规格名称；返回：回读标签后返回。"""
    cell = row.locator(CATEGORY_CELL)
    label = cell.locator(".next-select-values > em")
    field = cell.get_by_role("combobox")
    if label.count() != 1 or label.inner_text() != category:
        if field.get_attribute("aria-expanded") != "true":
            page_step(lambda: field.click(timeout=WAIT_MS), completed=lambda: field.get_attribute("aria-expanded") == "true")
        popup = page.locator(CATEGORY_POPUP)
        expect(popup).to_have_count(1, timeout=WAIT_MS)
        option = popup.get_by_title(category, exact=True)
        if option.count() != 1:
            getLogger(__name__).warning("SKU分类未唯一匹配：规格=%s，预期分类=%s，匹配数=%d", name, category, option.count())
            raise NeedsAttention(f"规格“{name}”未找到唯一的SKU分类“{category}”")
        page_step(lambda: option.click(timeout=WAIT_MS), completed=lambda: popup.count() == 0)
        expect(popup).to_have_count(0, timeout=WAIT_MS)
    expect(field).to_have_attribute("aria-expanded", "false", timeout=WAIT_MS)
    expect(label).to_have_count(1, timeout=WAIT_MS)
    expect(label).to_have_text(category, timeout=WAIT_MS)
    expect(label).to_have_attribute("title", category, timeout=WAIT_MS)


def _verify_sku_detail(row, detail: dict[str, str], category: str, *, layered=False) -> None:
    """核对整行SKU；参数：规格行、预期明细、分类；返回：全部字段一致后返回。"""
    if not layered:
        expect(row.locator(CREATED_NAMES)).to_have_value(detail["销售规格"], timeout=WAIT_MS)
    for key, selector in DETAIL_CELLS.items():
        expected = detail[key] if key == "商家编码" else _numeric_pattern(detail[key])
        expect(row.locator(selector).get_by_role("textbox")).to_have_value(expected, timeout=WAIT_MS)
    label = row.locator(CATEGORY_CELL).locator(".next-select-values > em")
    expect(label).to_have_count(1, timeout=WAIT_MS)
    expect(label).to_have_text(category, timeout=WAIT_MS)
    expect(label).to_have_attribute("title", category, timeout=WAIT_MS)


@qianniu_operation
def fill_sku_details(page: Page, details: list[dict[str, str]], *, category: str = "单品", combinations=None) -> list[dict[str, str]]:
    """填写SKU明细；参数：商品页、已校验的Excel明细、默认分类；返回：按规格匹配并核对的明细。"""
    if not details or not isinstance(category, str) or not category.strip():
        raise ValueError("SKU明细及分类不能为空")
    required = ("销售规格", *DETAIL_CELLS)
    for detail in details:
        if any(not isinstance(detail.get(key), str) or not detail[key] for key in required):
            raise ValueError("SKU明细需包含文本规格、价格、数量、商家编码，请先校验Excel")
    names = [detail["销售规格"] for detail in details]
    if len(set(names)) != len(names):
        raise ValueError("Excel销售规格重复，无法唯一匹配SKU行")
    logger = getLogger(__name__)
    logger.info("开始填写SKU明细：共%d项；SKU分类=%s", len(details), category)
    group = page.locator(SKU_GROUP)
    expect(group).to_be_visible(timeout=WAIT_MS)
    rows = group.locator(SKU_ROWS)
    expect(rows.first).to_be_visible(timeout=WAIT_MS)
    def matched_rows(only=None):
        if combinations:
            return layered_sku_rows(group, combinations, only=only)
        actual_names = []
        for row in rows.all():
            name_field = row.locator(CREATED_NAMES)
            expect(name_field).to_have_count(1, timeout=WAIT_MS)
            actual_names.append(name_field.input_value())
        if set(actual_names) != set(names) or len(actual_names) != len(names):
            logger.warning("SKU规格无法一一匹配：Excel=%r；页面=%r；明细尚未填写", names, actual_names)
            raise NeedsAttention("页面SKU规格与Excel无法一一匹配，请核对数量、名称及页面是否完整显示")
        return {name: rows.nth(index) for index, name in enumerate(actual_names)}

    matching = matched_rows()
    # 先检查全部对应字段和长度，避免填到一半才发现后续单元格无法接收数据。
    for detail in details:
        row = matching[detail["销售规格"]]
        for key, selector in DETAIL_CELLS.items():
            field = row.locator(selector).get_by_role("textbox")
            expect(field).to_have_count(1, timeout=WAIT_MS)
            limit = field.get_attribute("maxlength")
            if limit and len(detail[key]) > int(limit):
                raise ValueError(f"规格“{detail['销售规格']}”的{key}超过页面{limit}字符限制")
        expect(row.locator(CATEGORY_CELL).get_by_role("combobox")).to_have_count(1, timeout=WAIT_MS)
    guide = group.get_by_role("button", name="我知道了", exact=True)
    if guide.count() == 1 and guide.is_visible():
        page_step(lambda: guide.click(timeout=WAIT_MS), completed=lambda: not guide.is_visible())
    preview = page.locator(".preview-pannel.show:not(.collapsed) .header svg.expand")
    if preview.count() == 1 and preview.is_visible():
        page_step(lambda: preview.click(timeout=WAIT_MS), completed=lambda: not preview.is_visible())
    for index, detail in enumerate(details, start=1):
        name = detail["销售规格"]
        checkpoint(f"填写SKU明细 {index}/{len(details)}：{name}")
        row = matched_rows(only=name)[name] if combinations else matching[name]
        if not combinations:
            expect(row.locator(CREATED_NAMES)).to_have_value(name, timeout=WAIT_MS)
        for key, selector in DETAIL_CELLS.items():
            if combinations:
                row = matched_rows(only=name)[name]
            _fill_sku_field(row.locator(selector).get_by_role("textbox"), detail[key], key != "商家编码")
        if combinations:
            row = matched_rows(only=name)[name]
        _select_sku_category(page, row, category, name)
        if combinations:
            row = matched_rows(only=name)[name]
        errors = row.locator(".sell-o-message:visible, [role='alert']:visible")
        messages = [message.strip() for message in errors.all_text_contents() if message.strip()]
        if messages:
            logger.warning("SKU明细存在平台提示：规格=%s；提示=%r；当前行未报告完成", name, messages)
            raise NeedsAttention(f"规格“{name}”存在平台提示，请核对：" + "；".join(messages))
        _verify_sku_detail(row, detail, category, layered=bool(combinations))
        logger.info("SKU明细已按规格匹配并回读核对：规格=%s；价格=%s；数量=%s；商家编码=%s；SKU分类=%s",
                    name, detail["价格"], detail["数量"], detail["商家编码"], category)
    for detail in details:
        checkpoint(f"最终核对SKU明细：{detail['销售规格']}")
        name = detail["销售规格"]
        _verify_sku_detail(matched_rows(only=name)[name] if combinations else matching[name],
                           detail, category, layered=bool(combinations))
    logger.info("全部SKU明细填写完成：共%d项，按规格名称匹配", len(details))
    return details
