"""列表销量的共享解析契约：保存展示数量，不推断真实成交量。"""
from decimal import Decimal, InvalidOperation, localcontext
import re
import unicodedata

PARSER_VERSION = 1
TAOBAO_LIST_PARSER_VERSION = 2
MAX_COUNT = 18446744073709551615
FIELDS = ('sales_count', 'sales_precision', 'sales_metric', 'sales_period', 'sales_parse_status')
PRECISIONS = {'exact': '精确展示', 'lower_bound': '数量下界', 'approximate': '近似数量', 'unknown': '未知'}
METRICS = {'paid_people': '付款人数', 'received_people': '收货人数', 'sold_units': '已售件数', 'unknown': '未知口径'}
PERIODS = {'cumulative': '明确累计', 'monthly': '月度', 'last_30_days': '近30天', 'unknown': '未知周期'}
STATUSES = {'parsed': '已解析', 'no_quantity': '无数量', 'unrecognized': '无法识别', 'not_provided': '未提供', 'pending': '待解析'}
PATTERN = re.compile(r'(?P<prefix>累计|近30天|近30日|最近30天|本月|月售|月销|已售|售出|销量)?\s*'
                     r'(?P<approx>约)?\s*(?P<number>(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)\s*'
                     r'(?P<unit>万|亿|千)?\s*(?P<plus>\+|以上)?\s*'
                     r'(?P<suffix>人付款|人已付款|人收货|人已收货|件已售|件|人)?')


def parse_sales(raw):
    """保守解析原文；参数：销量文本或空；返回：五个结构化字段，不修改原文。"""
    result = dict(sales_count=None, sales_precision='unknown', sales_metric='unknown', sales_period='unknown',
                  sales_parse_status='not_provided')
    if raw is None or isinstance(raw, str) and not raw.strip():
        return result
    if not isinstance(raw, str):
        raise ValueError('销量原文必须为文本或空')
    text = unicodedata.normalize('NFKC', raw).strip()
    period_markers = [code for code, expression in (
        ('cumulative', r'累计'), ('monthly', r'本月|月售|月销'), ('last_30_days', r'近30天|近30日|最近30天'))
        if re.search(expression, text)]
    if len(period_markers) == 1:
        result['sales_period'] = period_markers[0]
    match = PATTERN.fullmatch(text)
    if match is None or len(period_markers) > 1:
        quantity_text=re.sub(r'最近30天|近30天|近30日','',text)
        result['sales_parse_status'] = ('no_quantity' if len(period_markers)<=1 and not re.search(r'[0-9]', quantity_text)
                                        else 'unrecognized')
        return result
    try:
        with localcontext() as context:
            context.prec=max(40,len(match['number'])+10)
            number = Decimal(match['number'].replace(',', '')) * {'万': 10000, '亿': 100000000, '千': 1000, None: 1}[match['unit']]
        if number != number.to_integral_value() or not 0 <= number <= MAX_COUNT or ('.' in match['number'] and not match['unit']):
            raise ValueError('数量无法保存为有效整数')
    except (InvalidOperation, ValueError):
        result['sales_parse_status'] = 'unrecognized'
        return result
    # “约1000+”同时声明近似和下界，含义冲突时不制造可靠下界。
    if match['approx'] and match['plus']:
        result['sales_parse_status'] = 'unrecognized'
        return result
    suffix = match['suffix']
    metric = ('paid_people' if suffix in {'人付款', '人已付款'} else 'received_people'
              if suffix in {'人收货', '人已收货'} else 'sold_units' if suffix in {'件', '件已售'} else 'unknown')
    result.update(sales_count=int(number), sales_metric=metric, sales_parse_status='parsed',
                  sales_precision='lower_bound' if match['plus'] else 'approximate' if match['approx'] or match['unit'] else 'exact')
    return result


def sales_filters(options):
    """校验网页销量范围和口径；参数：筛选字典；返回：规范值，不接受负数或小数。"""
    result = {}
    for name in ('sales_min', 'sales_max'):
        raw = options.get(name)
        if raw is None or raw == '':
            result[name] = None
        elif isinstance(raw, bool) or not re.fullmatch(r'[0-9]{1,20}', str(raw)) or int(raw) > MAX_COUNT:
            raise ValueError('销量范围须为有效的非负整数')
        else:
            result[name] = int(raw)
    if result['sales_min'] is not None and result['sales_max'] is not None and result['sales_min'] > result['sales_max']:
        raise ValueError('销量最小值不能大于最大值')
    for name, allowed in (('sales_metric', METRICS), ('sales_period', PERIODS), ('sales_precision', PRECISIONS)):
        value = options.get(name, '')
        if value and value not in allowed:
            raise ValueError('销量筛选条件无效')
        result[name] = value
    return result


def taobao_received_period_update(values):
    """生成淘宝搜索列表周期更正；参数：含原文及五字段的行；返回：仅周期补丁，调用方须核对来源。"""
    if (values.get('sales_period') != 'unknown' or values.get('sales_metric') != 'received_people'
            or values.get('sales_parse_status') != 'parsed'):
        return {}
    parsed = parse_sales(values.get('sales_text'))
    if all(values.get(name) == parsed[name] for name in FIELDS):
        return {'sales_period': 'last_30_days'}
    return {}


def parse_taobao_list_sales(raw, *, version=TAOBAO_LIST_PARSER_VERSION):
    """解析淘宝搜索列表；参数：原文和规则版本；返回：五字段，V2按用户确认补收货人数近30天周期。"""
    if type(version) is not int or version not in (PARSER_VERSION, TAOBAO_LIST_PARSER_VERSION):
        raise ValueError('列表销量解析规则版本不支持')
    result = parse_sales(raw)
    if version == TAOBAO_LIST_PARSER_VERSION:
        result.update(taobao_received_period_update({'sales_text': raw, **result}))
    return result
