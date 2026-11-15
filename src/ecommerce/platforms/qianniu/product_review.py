"""在提交前只读核对商品编辑页，不触发填写或保存。"""
from decimal import Decimal, InvalidOperation

from ecommerce.shared.exceptions import NeedsAttention
from .product_attributes import ATTRIBUTE_GROUP
from .product_basic import TITLE_GROUP, GUIDE_TITLE_GROUP, FIXED_PRICE_GROUP, MERCHANT_CODE_GROUP
from .product_details import IMAGES
from .product_images import _image_group
from .product_sku import SKU_GROUP, SKU_ROWS, CREATED_NAMES, DETAIL_CELLS, layered_sku_rows
from .product_sku_media import SEARCH_IMAGE, SPEC_IMAGE, TITLE
from .product_video import VIDEO_GROUP
from .verification import qianniu_operation

ATTRIBUTES = ('适用场景', '适用宠物类型', '握柄形态', '粘面材质', '粘性强度')


def _number(value):
    """规范化页面数字；参数：金额或库存文本；返回：可比较文本。"""
    try:
        number = Decimal(value)
        return format(number.normalize(), 'f') if number.is_finite() else value
    except InvalidOperation:
        return value


@qianniu_operation
def read_listing_media(page, group):
    """读取已填媒体来源及顺序；参数：编辑页和固定分组；返回：来源列表。"""
    if group in ('main', 'portrait'):
        images = _image_group(page, '1:1' if group == 'main' else '3:4').locator('.drag-item img')
    elif group == 'detail':
        images = page.locator(IMAGES)
    elif group == 'video':
        images = page.locator(VIDEO_GROUP).locator('img.cover')
    else:
        raise ValueError('未知的商品媒体分组')
    return images.evaluate_all("els => els.map(e => e.getAttribute('src') || '')")


def _input_value(locator, label, *, optional=False, strip=True):
    """读取编辑页中唯一对应的输入框。"""
    count = locator.count()
    if count == 0 and optional:
        return ''
    if count != 1:
        raise NeedsAttention(f'无法唯一核对{label}，不会自动提交')
    text = locator.input_value(timeout=3000)
    return text.strip() if strip else text


def _product_fields(page):
    return {'title': _input_value(page.locator(TITLE_GROUP).get_by_role('textbox'), '宝贝标题', strip=False),
            'code': _input_value(page.locator(MERCHANT_CODE_GROUP).get_by_role('textbox'), '商家编码')}


@qianniu_operation
def read_product_fields(page):
    """从编辑页读取标题及商品级商家编码。"""
    return _product_fields(page)


@qianniu_operation
def read_listing_fields(page, *, attribute_labels=ATTRIBUTES, sku_combinations=None):
    """读取实际商品和SKU；参数：编辑页；返回：结构化当前值。"""
    result = _product_fields(page)
    result['guide'] = _input_value(page.locator(GUIDE_TITLE_GROUP).get_by_role('textbox'), '导购标题',
                                   optional=True, strip=False)
    result['price'] = _number(_input_value(page.locator(FIXED_PRICE_GROUP).get_by_role('textbox'), '一口价'))
    result['attributes'] = {}
    for label in attribute_labels:
        group = page.locator(ATTRIBUTE_GROUP).filter(has=page.get_by_title(label, exact=True))
        field = group.get_by_role('combobox')
        if field.count() != 1:
            result['attributes'][label] = None
        elif field.get_attribute('readonly') is None:
            result['attributes'][label] = _input_value(field, label)
        else:
            values = group.locator('.next-select-values > em').all_text_contents()
            result['attributes'][label] = values[0].strip() if len(values) == 1 else None
    result['skus'], result['sku_media'] = [], []
    group = page.locator(SKU_GROUP)
    if sku_combinations:
        matching = layered_sku_rows(group, sku_combinations)
        named_rows = [(name, matching[name]) for name in sku_combinations]
    else:
        named_rows = [(_input_value(row.locator(CREATED_NAMES), '销售规格'), row)
                      for row in group.locator(SKU_ROWS).all()]
    for name, row in named_rows:
        info = {label: _input_value(row.locator(selector).get_by_role('textbox'), f'{name}的{label}')
                for label, selector in DETAIL_CELLS.items()}
        info.update({'销售规格': name, '价格': _number(info['价格']), '数量': _number(info['数量'])})
        result['skus'].append(info)
        pictures = f'{SEARCH_IMAGE} img' if sku_combinations else f'{SEARCH_IMAGE} img, {SPEC_IMAGE} img'
        result['sku_media'].append([name, row.locator(TITLE).all_inner_texts(),
            row.locator(pictures).evaluate_all("els=>els.map(e=>e.getAttribute('src'))")])
    if not result['code'] or not result['skus']:
        raise NeedsAttention('商家编码或 SKU 无法核对，未自动提交')
    try:
        numeric_ok = (Decimal(result['price']) > 0 and all(
            Decimal(row['价格']) > 0 and row['数量'].isdigit() and row['商家编码'] and row['销售规格']
            for row in result['skus']))
    except InvalidOperation:
        numeric_ok = False
    if not numeric_ok:
        raise NeedsAttention('页面价格、库存或 SKU 必填项无效，未自动提交')
    return result
