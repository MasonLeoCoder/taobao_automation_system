"""千牛自定义销售规格创建、SKU明细填写及回读核对。"""
import re
from decimal import Decimal, InvalidOperation
from logging import getLogger
from playwright.sync_api import Page
from ecommerce.shared.exceptions import NeedsAttention
from .verification import checkpoint, qianniu_operation, expect, page_step

SKU_GROUP = "#sell-field-sku"
DRAWER = ".sku-decouple-drawer:visible"
CREATED_NAMES = ".sku-unique-title-textarea textarea[data-real='true']"
WAIT_MS = 30000
SKU_ROWS = "tr.sku-table-row"
DETAIL_CELLS = {"价格": "td[id$='-skuPrice']", "数量": "td[id$='-skuStock']", "商家编码": "td[id$='-skuOuterId']"}
CATEGORY_CELL = "td[id$='-skuQuality']"
CATEGORY_POPUP = ".sell-o-select-popup-overlay:visible"


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
    custom = drawer.get_by_role("radio", name="自定义填写规格", exact=True)
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


def _verify_sku_detail(row, detail: dict[str, str], category: str) -> None:
    """核对整行SKU；参数：规格行、预期明细、分类；返回：全部字段一致后返回。"""
    expect(row.locator(CREATED_NAMES)).to_have_value(detail["销售规格"], timeout=WAIT_MS)
    for key, selector in DETAIL_CELLS.items():
        expected = detail[key] if key == "商家编码" else _numeric_pattern(detail[key])
        expect(row.locator(selector).get_by_role("textbox")).to_have_value(expected, timeout=WAIT_MS)
    label = row.locator(CATEGORY_CELL).locator(".next-select-values > em")
    expect(label).to_have_count(1, timeout=WAIT_MS)
    expect(label).to_have_text(category, timeout=WAIT_MS)
    expect(label).to_have_attribute("title", category, timeout=WAIT_MS)


@qianniu_operation
def fill_sku_details(page: Page, details: list[dict[str, str]], *, category: str = "单品") -> list[dict[str, str]]:
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
    actual_names = []
    for index in range(rows.count()):
        name_field = rows.nth(index).locator(CREATED_NAMES)
        expect(name_field).to_have_count(1, timeout=WAIT_MS)
        actual_names.append(name_field.input_value())
    if len(set(actual_names)) != len(actual_names) or set(actual_names) != set(names) or len(actual_names) != len(names):
        logger.warning("SKU规格无法一一匹配：Excel=%r；页面=%r；明细尚未填写", names, actual_names)
        raise NeedsAttention("页面SKU规格与Excel无法一一匹配，请核对数量、名称及页面是否完整显示")
    row_indices = {name: index for index, name in enumerate(actual_names)}
    # 先检查全部对应字段和长度，避免填到一半才发现后续单元格无法接收数据。
    for detail in details:
        row = rows.nth(row_indices[detail["销售规格"]])
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
        row = rows.nth(row_indices[name])
        expect(row.locator(CREATED_NAMES)).to_have_value(name, timeout=WAIT_MS)
        for key, selector in DETAIL_CELLS.items():
            _fill_sku_field(row.locator(selector).get_by_role("textbox"), detail[key], key != "商家编码")
        _select_sku_category(page, row, category, name)
        errors = row.locator(".sell-o-message:visible, [role='alert']:visible")
        messages = [message.strip() for message in errors.all_text_contents() if message.strip()]
        if messages:
            logger.warning("SKU明细存在平台提示：规格=%s；提示=%r；当前行未报告完成", name, messages)
            raise NeedsAttention(f"规格“{name}”存在平台提示，请核对：" + "；".join(messages))
        _verify_sku_detail(row, detail, category)
        logger.info("SKU明细已按规格匹配并回读核对：规格=%s；价格=%s；数量=%s；商家编码=%s；SKU分类=%s",
                    name, detail["价格"], detail["数量"], detail["商家编码"], category)
    for detail in details:
        checkpoint(f"最终核对SKU明细：{detail['销售规格']}")
        _verify_sku_detail(rows.nth(row_indices[detail["销售规格"]]), detail, category)
    logger.info("全部SKU明细填写完成：共%d项，按规格名称匹配", len(details))
    return details
