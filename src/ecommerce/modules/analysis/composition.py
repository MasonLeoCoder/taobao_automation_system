"""价位内展示档位汇总；只统计当前数量口径，保留精确、下界和近似的区别。"""
from collections import Counter
from decimal import Decimal
import re

from ecommerce.shared.sales import MAX_COUNT, METRICS, PERIODS


def _label(count, precision, metric):
    """格式化展示档位；参数：人数/件数、精度和指标；返回：带原精度含义的标签。"""
    unit = '人' if metric in ('received_people','paid_people') else '件' if metric=='sold_units' else ''
    if count >= 10000:
        divisor, suffix = (100000000,'亿') if count >= 100000000 else (10000,'万')
        text = format(Decimal(count)/divisor,'f').rstrip('0').rstrip('.') if count % divisor else str(count//divisor)
        text += suffix
    else:
        text = str(count)
    return ('约' if precision=='approximate' else '') + text + ('+' if precision=='lower_bound' else '') + unit


def parse_quantity_tier(raw):
    """校验档位筛选；参数：URL标识；返回：完整口径/精度/数值，空值返回空。"""
    if not raw:
        return None
    if not isinstance(raw,str) or len(raw)>110:
        raise ValueError('数量展示档位格式无效')
    parts = raw.split(':')
    if (len(parts)!=4 or parts[0] not in METRICS or parts[1] not in PERIODS
            or parts[2] not in ('exact','lower_bound','approximate')
            or not re.fullmatch(r'0|[1-9][0-9]{0,19}',parts[3]) or int(parts[3])>MAX_COUNT):
        raise ValueError('数量展示档位格式无效')
    metric, period, precision, count = parts
    return {'key':raw, 'group':metric+':'+period, 'precision':precision, 'value':int(count),
            'label':_label(int(count),precision,metric), 'group_label':METRICS[metric]+' · '+PERIODS[period]}


def quantity_tiers(rows, group):
    """汇总真实展示档位；参数：样本及数量口径；返回：保留精度、按数值排序的档位。"""
    comparable = [r for r in rows if r['quantity_usable'] and r['sales_group']==group]
    counts = Counter((r['sales_count'],r['sales_precision']) for r in comparable)
    ordered = sorted(counts,key=lambda pair:(pair[0],('exact','lower_bound','approximate').index(pair[1])))
    tiers = []
    for index,(value,precision) in enumerate(ordered):
        key = f'{group}:{precision}:{value}'
        tiers.append({**parse_quantity_tier(key),'count':counts[value,precision],
                      'tone':round(index*4/(len(ordered)-1)) if len(ordered)>1 else 2})
    return tiers


def common_received_tiers(rows):
    """计算最常见收货档位；参数：当前完整样本；返回：近30天有效商品分母及全部并列档位。"""
    tiers = quantity_tiers(rows, 'received_people:last_30_days')
    total = sum(tier['count'] for tier in tiers)
    maximum = max((tier['count'] for tier in tiers),default=0)
    return {'total':total, 'tiers':[{**tier,'percent':float(Decimal(tier['count'])*100/total)}
                                  for tier in tiers if tier['count']==maximum]}


def price_composition(rows, bins, group):
    """按价位汇总展示档位；参数：完整样本、既有价格段和数量口径；返回：行、统一色标和合计。"""
    comparable = [r for r in rows if r['price_usable'] and r['quantity_usable'] and r['sales_group']==group]
    tiers = quantity_tiers(comparable,group)
    items = []
    for item in bins:
        members = [r for r in comparable if r['price_bin']==item['index']]
        bucket = Counter((r['sales_count'],r['sales_precision']) for r in members)
        items.append({**item,'tiers':[{**tier,'count':bucket[tier['value'],tier['precision']]} for tier in tiers
                                     if bucket[tier['value'],tier['precision']]],
                      'unavailable':item['count']-len(members)})
    total = sum(item['count'] for item in bins)
    return {'rows':items,'tiers':tiers,'total':total,'comparable':len(comparable),
            'unavailable':total-len(comparable),'missing_prices':len(rows)-total,
            'metric_label':METRICS.get(group.split(':')[0],'数量')}
