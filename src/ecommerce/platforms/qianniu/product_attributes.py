"""千牛商品属性填写、选择及按明确规则回退。"""
from dataclasses import dataclass
from logging import getLogger
from typing import Any
from playwright.sync_api import Page
from ecommerce.shared.exceptions import NeedsAttention
from .verification import qianniu_operation, expect, page_step

ATTRIBUTE_GROUP = ".sell-component-info-wrapper-wrap.sell-component-item-prop-item"
OPTIONS_POPUP = ".sell-o-select-popup-overlay:visible"
WAIT_MS = 30000


@dataclass(frozen=True)
class AttributeSelectionResult:
    """返回Excel原值、实际选择及回退原因；原因为空表示精确匹配。"""
    requested: Any
    selected: str
    fallback_reason: str | None


def _select_attribute(page: Page, field_name: str, value: Any) -> AttributeSelectionResult:
    """共用属性选择；参数：页面、字段名称、Excel原值；返回：选择及回退原因。"""
    logger = getLogger(__name__)
    group = page.locator(ATTRIBUTE_GROUP).filter(has=page.get_by_title(field_name, exact=True))
    expect(group).to_have_count(1, timeout=WAIT_MS)
    expect(group).to_be_visible(timeout=WAIT_MS)
    field = group.get_by_role("combobox")
    if field.get_attribute("aria-expanded") != "true":
        page_step(lambda: field.click(timeout=WAIT_MS), completed=lambda: field.get_attribute("aria-expanded") == "true")
    popup = page.locator(OPTIONS_POPUP)
    expect(popup).to_have_count(1, timeout=WAIT_MS)
    search = popup.get_by_role("textbox")
    if search.count() == 1 and search.input_value():
        page_step(lambda: search.fill(""))
    options = popup.locator(".options-item")
    # 该控件用自定义列表而不是role=option，读取实际显示名称，不自行拼接属性值。
    expect(options.first).to_be_visible(timeout=WAIT_MS)
    names = [name.strip() for name in options.locator(".info-content").all_text_contents()]
    if not names or any(not name for name in names) or len(names) != options.count():
        raise NeedsAttention(f"{field_name}没有可核对的下拉选项，无法默认第一项")
    requested = value.strip() if isinstance(value, str) else ""
    matches = [i for i, name in enumerate(names) if requested and name == requested]
    reason = None
    if len(matches) == 1:
        index = matches[0]
    else:
        index = 0
        if value is None or (isinstance(value, str) and not value.strip()):
            reason = f"Excel未提供{field_name}或值为空"
        elif not isinstance(value, str):
            reason = f"Excel{field_name}不是文本"
        elif not matches:
            reason = "Excel值未匹配到下拉选项"
        else:
            reason = f"匹配到{len(matches)}个同名选项，无法唯一匹配"
        logger.warning("%s准备回退至第一项：Excel原值=%r；原因=%s；第一项=%s", field_name, value, reason, names[0])
    selected = names[index]
    page_step(lambda: options.nth(index).click(timeout=WAIT_MS), completed=lambda: popup.count() == 0)
    page_step(field.blur)
    expect(popup).to_have_count(0, timeout=WAIT_MS)
    if field.get_attribute("readonly") is not None:
        # 只读选择器把真实选中值放在标签里，搜索input的value始终为空。
        label = group.locator(".next-select-values > em")
        expect(label).to_have_count(1, timeout=WAIT_MS)
        expect(label).to_have_text(selected, timeout=WAIT_MS)
        expect(label).to_have_attribute("title", selected, timeout=WAIT_MS)
    else:
        expect(field).to_have_value(selected, timeout=WAIT_MS)
    errors = group.locator(".sell-component-info-wrapper-msg-content.error:visible")
    if errors.count():
        raise NeedsAttention(f"{field_name}未通过平台校验：" + "；".join(errors.all_text_contents()))
    logger.info("%s已选择并回读核对：Excel原值=%r；实际选择=%s；回退原因=%s",
                field_name, value, selected, reason or "无，精确匹配")
    return AttributeSelectionResult(value, selected, reason)


# 保留已有场景结果导入方式，各属性字段共用同一结果结构。
ApplicableSceneResult = AttributeSelectionResult


@qianniu_operation
def select_applicable_scene(page: Page, value: Any = None) -> AttributeSelectionResult:
    """选择适用场景；参数：页面、Excel原值；返回：精确匹配或第一项回退结果。"""
    return _select_attribute(page, "适用场景", value)


@qianniu_operation
def select_pet_type(page: Page, value: Any = None) -> AttributeSelectionResult:
    """选择适用宠物类型；参数：页面、Excel原值；返回：精确匹配或第一项回退结果。"""
    return _select_attribute(page, "适用宠物类型", value)


@qianniu_operation
def select_handle_shape(page: Page, value: Any = None) -> AttributeSelectionResult:
    """选择握柄形态；参数：页面、Excel原值；返回：精确匹配或第一项回退结果。"""
    return _select_attribute(page, "握柄形态", value)


@qianniu_operation
def select_adhesive_material(page: Page, value: Any = None) -> AttributeSelectionResult:
    """选择粘面材质；参数：页面、Excel原值；返回：精确匹配或第一项回退结果。"""
    return _select_attribute(page, "粘面材质", value)


@qianniu_operation
def select_adhesion_strength(page: Page, value: Any = None) -> AttributeSelectionResult:
    """选择粘性强度；参数：页面、Excel原值；返回：精确匹配或第一项回退结果。"""
    return _select_attribute(page, "粘性强度", value)


@qianniu_operation
def fill_item_number(page: Page, item_number: str) -> None:
    """填写货号；参数：商品页、Excel文本货号；返回：完整值回读及平台校验后返回。"""
    if not isinstance(item_number, str) or not item_number.strip():
        raise ValueError("货号必须为非空文本，请检查Excel中的货号列")
    item_number = item_number.strip()
    group = page.locator(ATTRIBUTE_GROUP).filter(has=page.get_by_title("货号", exact=True))
    expect(group).to_have_count(1, timeout=WAIT_MS)
    expect(group).to_be_visible(timeout=WAIT_MS)
    field = group.get_by_role("combobox")
    expect(field).to_have_count(1, timeout=WAIT_MS)
    current = field.input_value()
    if current and current != item_number:
        raise NeedsAttention("页面已有不同的货号，请核对后处理；没有自动覆盖")
    if current != item_number:
        page_step(lambda: field.fill(item_number, timeout=WAIT_MS))
    # Tab触发组件失焦并关闭提示列表；直接blur可能让空提示层残留。
    page_step(lambda: field.press("Tab", timeout=WAIT_MS), completed=lambda: field.get_attribute("aria-expanded") == "false")
    expect(field).to_have_attribute("aria-expanded", "false", timeout=WAIT_MS)
    expect(field).to_have_value(item_number, timeout=WAIT_MS)
    errors = group.locator(".sell-component-info-wrapper-msg-content.error:visible")
    if errors.count():
        raise NeedsAttention("货号未通过平台校验：" + "；".join(errors.all_text_contents()))
    getLogger(__name__).info("货号已填写并回读核对：%s", item_number)
