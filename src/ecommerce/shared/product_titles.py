"""商品标题的字符计数和 SKU 搜索标题校验。"""

SKU_TITLE_MIN = 20
SKU_TITLE_MAX = 30


def title_width(value):
    """ASCII 字符计 1，其余字符计 2。"""
    return sum(1 if ord(char) < 128 else 2 for char in value)


def compact_title(value):
    """去掉仅用于分隔候选词组的空白。"""
    return ''.join(value.split())


def validate_sku_title(value):
    """返回实际填写的文本。空标题允许留空，非空标题须符合字符限制。"""
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('搜索标题必须为单行文本')
    value = compact_title(value)
    width = title_width(value)
    if width > SKU_TITLE_MAX:
        raise ValueError(f'搜索标题超过 {SKU_TITLE_MAX} 字符，当前 {width} / {SKU_TITLE_MAX}，超出 {width - SKU_TITLE_MAX} 字符')
    if value and width < SKU_TITLE_MIN:
        raise ValueError(f'搜索标题至少需要 {SKU_TITLE_MIN} 字符，当前 {width} / {SKU_TITLE_MAX}，还差 {SKU_TITLE_MIN - width} 字符')
    return value
