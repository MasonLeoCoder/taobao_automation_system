"""在提交前只读核对商品编辑页，不触发填写或保存。"""
from decimal import Decimal, InvalidOperation

from ecommerce.shared.exceptions import NeedsAttention
from .product_attributes import ATTRIBUTE_GROUP
from .product_basic import TITLE_GROUP, GUIDE_TITLE_GROUP, FIXED_PRICE_GROUP, MERCHANT_CODE_GROUP
from .product_details import IMAGES
from .product_images import _image_group
from .product_sku import SKU_GROUP, SKU_ROWS, CREATED_NAMES, DETAIL_CELLS
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


@qianniu_operation
def read_listing_fields(page):
    """读取实际商品和SKU；参数：编辑页；返回：结构化当前值。"""
    def value(locator, label, *, optional=False):
        """读取唯一文本框；参数：定位器、名称及可空标志；返回：当前值。"""
        count = locator.count()
        if count == 0 and optional:
            return ''
        if count != 1:
            raise NeedsAttention(f'无法唯一核对{label}，不会自动提交')
        return locator.input_value(timeout=3000).strip()

    result = {key: value(page.locator(group).get_by_role('textbox'), label, optional=key == 'guide')
              for key, group, label in (('title', TITLE_GROUP, '宝贝标题'), ('guide', GUIDE_TITLE_GROUP, '导购标题'),
              ('price', FIXED_PRICE_GROUP, '一口价'), ('code', MERCHANT_CODE_GROUP, '商家编码'))}
    result['price'] = _number(result['price'])
    result['attributes'] = {}
    for label in ATTRIBUTES:
        group = page.locator(ATTRIBUTE_GROUP).filter(has=page.get_by_title(label, exact=True))
        field = group.get_by_role('combobox')
        if field.count() != 1:
            result['attributes'][label] = None
        elif field.get_attribute('readonly') is None:
            result['attributes'][label] = value(field, label)
        else:
            values = group.locator('.next-select-values > em').all_text_contents()
            result['attributes'][label] = values[0].strip() if len(values) == 1 else None
    result['skus'], result['sku_media'] = [], []
    rows = page.locator(SKU_GROUP).locator(SKU_ROWS)
    for row in rows.all():
        name = value(row.locator(CREATED_NAMES), '销售规格')
        info = {label: value(row.locator(selector).get_by_role('textbox'), f'{name}的{label}')
                for label, selector in DETAIL_CELLS.items()}
        info.update({'销售规格': name, '价格': _number(info['价格']), '数量': _number(info['数量'])})
        result['skus'].append(info)
        result['sku_media'].append([name, row.locator(TITLE).all_text_contents(),
            row.locator(f'{SEARCH_IMAGE} img, {SPEC_IMAGE} img').evaluate_all("els=>els.map(e=>e.getAttribute('src'))")])
    if not result['title'] or not result['code'] or not result['skus']:
        raise NeedsAttention('商品标题、商家编码或 SKU 无法核对，未自动提交')
    try:
        numeric_ok = (Decimal(result['price']) > 0 and all(
            Decimal(row['价格']) > 0 and row['数量'].isdigit() and row['商家编码'] and row['销售规格']
            for row in result['skus']))
    except InvalidOperation:
        numeric_ok = False
    if not numeric_ok:
        raise NeedsAttention('页面价格、库存或 SKU 必填项无效，未自动提交')
    return result
