"""长期关注的输入校验、分页与对照提示，不触发采集。"""
from ecommerce.infrastructure.storage import favorites as repository
from ecommerce.shared.sales import METRICS, PERIODS


def identifier(value):
    """校验正整数身份；参数：外部值；返回：整数，拒绝布尔或越界。"""
    if type(value) is not int or not 0 < value < 2**64:
        raise ValueError('商品和采集记录编号必须为有效正整数')
    return value


def note_text(value):
    """校验人工备注；参数：原文；返回：普通文字，空字符串允许。"""
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError('备注最多500字')
    return value


def add(settings, product_id, observation_id):
    """关注已确认商品；参数：配置、商品、观察；返回：幂等关注记录。"""
    return repository.add(settings, identifier(product_id), identifier(observation_id))


def comparison(initial, latest):
    """解释两条观察的可比性；参数：加入时和最近记录；返回：时间、人数、价格提示。"""
    result = {'new_record': bool(initial and latest and initial['id'] != latest['id']),
              'quantity': '数据不足，无法比较人数', 'price': '价格数据不足，无法比较'}
    if not initial or not latest:
        return result
    if initial['id'] == latest['id']:
        result.update(quantity='加入后尚未重新采集', price='加入后尚未重新采集')
        return result
    same_group = all(initial.get(k) == latest.get(k) for k in ('sales_metric', 'sales_period'))
    comparable = same_group and initial.get('sales_parse_status') == latest.get('sales_parse_status') == 'parsed' and all(
        r.get('sales_precision') in {'exact', 'lower_bound'} and type(r.get('sales_count')) is int for r in (initial, latest))
    if not same_group:
        result['quantity'] = '人数统计口径或周期不同，不直接比较'
    elif not latest.get('sales_text'):
        result['quantity'] = '最近记录未提供人数，不表示降至0'
    elif comparable:
        delta = latest['sales_count'] - initial['sales_count']
        if delta == 0:
            result['quantity'] = '展示值未变；带“+”时实际人数仍可能变化'
        else:
            result['quantity'] = '人数展示值' + ('上升' if delta > 0 else '下降') + '；差额不代表新增或流失人数'
    if initial.get('price_value') is not None and latest.get('price_value') is not None:
        if initial.get('price_label') != latest.get('price_label'):
            result['price'] = '价格条件不同，需核对后比较'
        elif initial['price_value'] == latest['price_value']:
            result['price'] = '网页展示价格未变，商品规格仍需核对'
        else:
            result['price'] = '网页展示价格变化，不能直接认定为同规格涨价或降价'
    return result


def page(settings, options):
    """校验关注清单筛选并读取分页；参数：配置、URL条件；返回：清单与解释。"""
    filters = {}
    for key in ('q', 'keyword', 'shop'):
        value = options.get(key, '')
        if not isinstance(value, str) or len(value) > 255:
            raise ValueError('筛选文字最多255字')
        filters[key] = value.strip()
    filters['sort'] = options.get('sort', 'created_desc')
    if filters['sort'] not in {'created_desc', 'created_asc', 'latest_desc', 'latest_asc'}:
        raise ValueError('关注排序无效')
    raw_page = str(options.get('page', '1'))
    if not raw_page.isascii() or not raw_page.isdigit() or not 0 < int(raw_page) <= 10**8:
        raise ValueError('页码须为正整数')
    filters['page'] = int(raw_page)
    data = repository.read_page(settings, filters)
    filters['page'] = data['pagination']['page']
    for item in data['items']:
        item['comparison'] = comparison(item['initial'], item['latest'])
        latest_time = item['latest'].get('captured_at') if item['latest'] else None
        item['data_state'] = ('采集依据当前不可用' if not item['latest'] or not item['initial'] else
            '加入后有新的采集记录' if latest_time and latest_time > item['created_at'] else
            '已有比加入时依据更新的记录，采集于关注之前' if item['comparison']['new_record'] else '加入后尚未重新采集')
        for row in (item['initial'], item['latest']):
            if row:
                row['sales_group_label'] = METRICS.get(row.get('sales_metric'), '未知口径') + ' · ' + PERIODS.get(row.get('sales_period'), '未知周期')
    return dict(data, filters=filters)
