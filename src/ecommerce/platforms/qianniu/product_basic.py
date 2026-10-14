"""千牛基础信息字段填写与回读核对。"""
import re
from decimal import Decimal
from logging import getLogger
from playwright.sync_api import Page, expect
from ecommerce.shared.exceptions import NeedsAttention
from .verification import qianniu_operation

TITLE_GROUP = "#sell-field-title"
GUIDE_TITLE_GROUP = "#sell-field-shopping_title"
FIXED_PRICE_GROUP = "#sell-field-price"
MERCHANT_CODE_GROUP = "#sell-field-outerId"
MULTI_DISCOUNT_GROUP = "#sell-field-multiDiscountPromotion"
LISTING_TIME_GROUP = "#sell-field-startTime"
WAIT_MS = 30000


def _fill_title(page: Page, title: str, selector: str, field_name: str, limit: int, *, minimum: int = 0, replace: bool = False) -> None:
    """共用标题填写；参数：页面、原文、字段区域、名称、字符上限；返回：回读核对后返回。"""
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"{field_name}必须为非空文本")
    title = title.strip()
    group = page.locator(selector)
    expect(group).to_be_visible(timeout=WAIT_MS)
    field = group.get_by_role("textbox")
    expect(field).to_have_count(1, timeout=WAIT_MS)
    current = field.input_value()
    if current and current != title and not replace:
        raise NeedsAttention(f"页面已有不同的{field_name}，请核对后处理；没有自动覆盖")
    if current != title:
        field.fill(title, timeout=WAIT_MS)
    field.blur()
    expect(field).to_have_value(title, timeout=WAIT_MS)
    counter = group.locator(".content-after > span").first
    expect(counter).to_have_text(re.compile(rf"^\d+/{limit}$"), timeout=WAIT_MS)
    used, displayed_limit = map(int, counter.inner_text().split("/"))
    if used < minimum:
        raise NeedsAttention(f"{field_name}需要{minimum}～{limit}字符，当前为{used}；请修改Excel中的标题")
    if used > displayed_limit:
        raise ValueError(f"{field_name}超出页面限制：{used}/{displayed_limit}；请缩短Excel标题")
    errors = group.locator(".sell-component-info-wrapper-msg-content.error:visible")
    if errors.count():
        raise NeedsAttention(f"{field_name}未通过平台校验：" + "；".join(errors.all_text_contents()))
    getLogger(__name__).info("%s已填写并回读核对，页面计数：%s/%s", field_name, used, displayed_limit)


@qianniu_operation
def fill_product_title(page: Page, title: str) -> None:
    """填写宝贝标题；参数：商品页、Excel标题；返回：原值及页面60字符计数核对后返回。"""
    _fill_title(page, title, TITLE_GROUP, "宝贝标题", 60)


@qianniu_operation
def fill_guide_title(page: Page, title: str, *, replace: bool = False) -> None:
    """填写导购标题；参数：商品页、Excel导购标题；返回：原值、20～30字符计数及平台错误检查后返回；replace用于明确授权的更新。"""
    _fill_title(page, title, GUIDE_TITLE_GROUP, "导购标题", 30, minimum=20, replace=replace)


@qianniu_operation
def fill_fixed_price(page: Page, price: str) -> None:
    """覆盖填写一口价；参数：商品页、已校验的Excel金额文本；返回：失焦及金额回读核对后返回。"""
    if not isinstance(price, str) or not re.fullmatch(r"\d+(?:\.\d{1,2})?", price) or Decimal(price) <= 0:
        raise ValueError("一口价必须为大于0、最多两位小数的金额文本")
    group = page.locator(FIXED_PRICE_GROUP)
    expect(group).to_be_visible(timeout=WAIT_MS)
    field = group.get_by_role("textbox")
    expect(field).to_have_count(1, timeout=WAIT_MS)
    limit = field.get_attribute("maxlength")
    if limit and len(price) > int(limit):
        raise ValueError(f"一口价超过页面{limit}字符限制")
    current = field.input_value()
    # fill会先清除旧内容再输入，已有值也按用户要求覆盖。
    field.fill(price, timeout=WAIT_MS)
    field.press("Tab", timeout=WAIT_MS)
    whole, _, fraction = price.partition(".")
    fraction = fraction.rstrip("0")
    decimal = rf"\.{re.escape(fraction)}0*" if fraction else r"(?:\.0+)?"
    expect(field).to_have_value(re.compile(rf"^0*{re.escape(whole)}{decimal}$"), timeout=WAIT_MS)
    errors = group.locator(".sell-component-info-wrapper-msg-content.error:visible")
    if errors.count():
        raise NeedsAttention("一口价未通过平台校验：" + "；".join(errors.all_text_contents()))
    getLogger(__name__).info("一口价已覆盖填写并回读核对：原值=%r；Excel金额=%s；页面金额=%s", current, price, field.input_value())


@qianniu_operation
def fill_merchant_code(page: Page, code: str) -> None:
    """填写商品级商家编码；参数：商品页、Excel基础信息文本编码；返回：完整回读及平台校验后返回。"""
    if not isinstance(code, str) or not code.strip():
        raise ValueError("商品级商家编码必须为非空文本")
    code = code.strip()
    group = page.locator(MERCHANT_CODE_GROUP)
    expect(group).to_be_visible(timeout=WAIT_MS)
    field = group.get_by_role("textbox")
    expect(field).to_have_count(1, timeout=WAIT_MS)
    limit = field.get_attribute("maxlength")
    if limit and len(code) > int(limit):
        raise ValueError(f"商品级商家编码超过页面{limit}字符限制")
    previous = field.input_value()
    if previous != code:
        field.fill(code, timeout=WAIT_MS)
    field.press("Tab", timeout=WAIT_MS)
    expect(field).to_have_value(code, timeout=WAIT_MS)
    if limit:
        counter = group.locator(".next-input-len")
        expect(counter).to_have_count(1, timeout=WAIT_MS)
        expect(counter).to_have_text(re.compile(rf"^\d+/{limit}$"), timeout=WAIT_MS)
        used, maximum = map(int, counter.inner_text().split("/"))
        if used > maximum:
            raise ValueError(f"商品级商家编码超出页面限制：{used}/{maximum}")
    errors = group.locator(".sell-o-message:visible, .sell-component-info-wrapper-msg-content.error:visible")
    messages = [message.strip() for message in errors.all_text_contents() if message.strip()]
    if messages:
        getLogger(__name__).warning("商品级商家编码存在平台错误：%r", messages)
        raise NeedsAttention("商品级商家编码未通过平台校验：" + "；".join(messages))
    getLogger(__name__).info("商品级商家编码已填写并回读核对：原值=%r；实际编码=%s", previous, code)


@qianniu_operation
def enable_multi_item_discount(page: Page) -> None:
    """启用多件优惠；参数：商品页；返回：勾选、控件启用及平台校验通过后返回。"""
    group = page.locator(MULTI_DISCOUNT_GROUP)
    expect(group).to_be_visible(timeout=WAIT_MS)
    # 千牛把复选框图标也计入名称；限定区域后核对唯一复选框。
    checkbox = group.get_by_role("checkbox")
    expect(checkbox).to_have_count(1, timeout=WAIT_MS)
    # check确保已勾选，重复执行不会把已有勾选取消。
    checkbox.check(timeout=WAIT_MS)
    expect(checkbox).to_be_checked(timeout=WAIT_MS)
    promotion = group.get_by_role("combobox")
    discount = group.get_by_role("textbox")
    expect(promotion).to_have_count(1, timeout=WAIT_MS)
    expect(discount).to_have_count(1, timeout=WAIT_MS)
    expect(promotion).to_be_enabled(timeout=WAIT_MS)
    expect(discount).to_be_enabled(timeout=WAIT_MS)
    errors = group.locator(".sell-component-info-wrapper-msg-content.error:visible")
    expect(errors).to_have_count(0, timeout=WAIT_MS)
    getLogger(__name__).info("多件优惠已启用并核对：优惠类型=%s；当前折扣=%s折", promotion.get_attribute("aria-valuetext"), discount.input_value())


@qianniu_operation
def select_warehouse_listing(page: Page) -> None:
    """选择上架时间为放入仓库；参数：商品页；返回：选中状态及单选互斥核对后返回。"""
    group = page.locator(LISTING_TIME_GROUP)
    expect(group).to_be_visible(timeout=WAIT_MS)
    warehouse = group.get_by_role("radio", name="放入仓库", exact=True)
    expect(warehouse).to_have_count(1, timeout=WAIT_MS)
    warehouse.check(timeout=WAIT_MS)
    expect(warehouse).to_be_checked(timeout=WAIT_MS)
    checked_count = group.get_by_role("radio").evaluate_all("els => els.filter(e => e.checked).length")
    if checked_count != 1:
        raise NeedsAttention("上架时间未形成唯一的单选状态，请核对页面")
    errors = group.locator(".sell-component-info-wrapper-msg-content.error:visible")
    expect(errors).to_have_count(0, timeout=WAIT_MS)
    getLogger(__name__).info("上架时间已选择并核对：放入仓库；尚未提交商品")
