"""入口价自动分段：保留全部有效价格，主体与尾部使用同一套精确边界。"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

MAX_PRICE_BINS = 7


def quantile(values, fraction):
    """计算线性插值分位数；参数：已排序Decimal及分位；返回：数值或空。"""
    if not values:
        return None
    position = Decimal(len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def _number(value):
    """去除金额末尾无意义零；参数：Decimal；返回：不使用科学计数法的文字。"""
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _interval(low, high, *, kind='regular', include_low=True, include_high=False):
    """建立唯一边界定义；参数：上下限、类型与闭合方式；返回：价格段字典。"""
    point = low == high
    return {'low': low, 'high': high, 'include_low': include_low,
            'include_high': include_high or point, 'kind': 'point' if point else kind}


def contains_price(price, item):
    """判断价格是否属于该段；参数：有效价格和边界定义；返回：布尔值。"""
    return ((price >= item['low'] if item['include_low'] else price > item['low'])
            and (price <= item['high'] if item['include_high'] else price < item['high']))


def _regular_bins(values, target, low_tail, high_tail):
    """生成主体整齐分段；参数：主体价格、目标段数及尾部存在标记；返回：边界列表。"""
    if values[0] == values[-1]:
        return [_interval(values[0], values[0], include_high=True)]
    raw_step = (values[-1] - values[0]) / target
    exponent = raw_step.adjusted()
    candidates = []
    tails = int(low_tail) + int(high_tail)
    for power in range(exponent - 1, exponent + 2):
        for multiplier in ('1', '2', '2.5', '4', '5'):
            step = Decimal(multiplier) * (Decimal(10) ** power)
            start = (values[0] / step).to_integral_value(rounding=ROUND_FLOOR) * step
            stop = (values[-1] / step).to_integral_value(rounding=ROUND_CEILING) * step
            count = int((stop - start) / step)
            if not 1 <= count <= MAX_PRICE_BINS - tails:
                continue
            candidates.append((abs(count - target), abs(step - raw_step), step, start, count))
    _, _, step, start, count = min(candidates)
    edges = [start + step * index for index in range(count + 1)]
    # 主体与尾部按真实主体端点相接，向外取整不得吞入尾部商品。
    if low_tail:
        edges[0] = values[0]
    if high_tail:
        edges[-1] = values[-1]
    return [_interval(low, high, include_high=index == count - 1)
            for index, (low, high) in enumerate(zip(edges, edges[1:])) if low < high]


def price_distribution(prices):
    """生成价格分布；参数：非负有限Decimal列表；返回：边界、计数及计算方式，不修改输入。"""
    values = sorted(prices)
    if not values:
        return {'bins': [], 'mode': 'empty', 'description': '暂无有效入口价，暂不生成价格分段。'}
    unique = sorted(set(values))
    low_tail, high_tail = [], []
    if len(unique) <= 6:
        bins = [_interval(value, value, include_high=True) for value in unique]
        mode = 'points'
        description = '价格种类较少，按实际价位分组。' if len(unique) > 1 else '全部有效入口价相同，显示一个价位。'
    else:
        q1, q3 = quantile(values, Decimal('.25')), quantile(values, Decimal('.75'))
        spread = (q3 - q1) * Decimal('1.5')
        lower, upper = q1 - spread, q3 + spread
        low_tail = [value for value in values if value < lower]
        high_tail = [value for value in values if value > upper]
        core = [value for value in values if lower <= value <= upper]
        bins = _regular_bins(core, 6 - bool(low_tail) - bool(high_tail), bool(low_tail), bool(high_tail))
        if low_tail:
            bins.insert(0, _interval(values[0], core[0], kind='low_tail'))
        if high_tail:
            bins.append(_interval(core[-1], values[-1], kind='high_tail', include_low=False, include_high=True))
        mode = 'tails' if low_tail or high_tail else 'equal_width'
        description = ('按四分位距的1.5倍识别低价/高价尾部，主体自动分段；尾部全部保留。'
                       if mode == 'tails' else '按当前样本自动分段，使用易读段宽；首尾覆盖实际最低和最高价。')
    for index, item in enumerate(bins):
        members = [value for value in values if contains_price(value, item)]
        item.update(index=index, count=len(members), percent=round(len(members) * 100 / len(values), 1),
                    actual_min=min(members) if members else None, actual_max=max(members) if members else None)
        if item['kind'] in ('low_tail', 'high_tail'):
            actual_range = _number(item['actual_min']) if item['actual_min'] == item['actual_max'] else _number(item['actual_min']) + '–' + _number(item['actual_max'])
            item['label'] = ('低价尾部' if item['kind'] == 'low_tail' else '高价尾部') + ' · ' + actual_range + '元'
        else:
            item['label'] = (_number(item['low']) if item['low'] == item['high'] else _number(item['low']) + '–' + _number(item['high'])) + '元'
        item['boundary_label'] = (f"价格 = {_number(item['low'])}元" if item['low'] == item['high'] else
            f"{_number(item['low'])} {'≤' if item['include_low'] else '<'} 价格 {'≤' if item['include_high'] else '<'} {_number(item['high'])}元")
    # 同时核对每项归属和总数，防止边界重叠与漏计互相抵消。
    if any(sum(contains_price(value, item) for item in bins) != 1 for value in unique):
        raise ValueError('价格分段边界校验失败')
    if sum(item['count'] for item in bins) != len(values):
        raise ValueError('价格分段数量校验失败')
    return {'bins': bins, 'mode': mode, 'description': description}
