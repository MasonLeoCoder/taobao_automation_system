"""搜索词样本分析：保留口径、观察归属及未知值，不推算市场销量。"""
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

from ecommerce.infrastructure.storage.search_analysis import load_search_sample
from ecommerce.shared.sales import METRICS, PERIODS, PRECISIONS
from ecommerce.shared.pagination import pagination, PAGE_SIZE
from ecommerce.modules.analysis.prices import MAX_PRICE_BINS, contains_price, price_distribution, quantile
from ecommerce.modules.analysis.composition import parse_quantity_tier, price_composition


def _text(value, maximum=200):
    """校验文本筛选；参数：输入及最大长度；返回：去空白文本。"""
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'筛选文字最多{maximum}字')
    return value.strip()


def parse_filters(options):
    """校验分析输入；参数：网页或其他入口的筛选；返回：规范化条件与日期边界。"""
    values = {key: _text(options.get(key, ''), 100 if key == 'keyword' else 400 if key == 'store' else 200)
              for key in ('keyword', 'snapshot', 'since', 'until', 'q', 'group', 'store')}
    if values['snapshot'] and not re.fullmatch(r'range|latest|task:[a-f0-9]{32}|batch:[1-9][0-9]{0,19}', values['snapshot']):
        raise ValueError('采集快照格式无效')
    values['exclude_ads'] = options.get('exclude_ads', '')
    if values['exclude_ads'] not in ('', '0', '1'):
        raise ValueError('广告筛选无效')
    values['exclude_ads'] = values['exclude_ads'] == '1'
    for key, allowed, default in (('view', {'goods', 'stores', 'saved'}, 'goods'),
                                  ('sort', {'position', 'price_asc', 'price_desc'}, 'position'),
                                  ('table_group', {'all', 'missing', *[m + ':' + p for m in METRICS for p in PERIODS]}, 'all'),
                                  ('finding', {'', 'high'}, '')):
        values[key] = options.get(key, default)
        if key == 'finding' and values[key] == 'unclassified':
            values[key] = ''  # 已移除的旧类型参数不再限制分析结果。
        if values[key] not in allowed:
            raise ValueError('分析筛选无效：' + key)
    raw_bin = options.get('bin', '')
    if raw_bin not in ('', *map(str, range(MAX_PRICE_BINS))):
        raise ValueError('价格段筛选无效')
    values['bin'] = int(raw_bin) if raw_bin else None
    values['quantity_tier'] = options.get('quantity_tier','')
    parse_quantity_tier(values['quantity_tier'])
    values['price_scale'] = options.get('price_scale', '')
    if not isinstance(values['price_scale'], str) or not re.fullmatch(r'(?:[a-f0-9]{16})?', values['price_scale']):
        raise ValueError('价格分段标识无效')
    raw_page = str(options.get('page', '1'))
    if not re.fullmatch(r'[1-9][0-9]{0,8}', raw_page):
        raise ValueError('页码须为正整数')
    values['page'] = int(raw_page)
    saved = options.get('saved', '')
    if not isinstance(saved, str) or len(saved) > 8000:
        raise ValueError('关注商品参数过长')
    values['saved'] = saved.split(',') if saved else []
    if any(not re.fullmatch(r'[po]:[1-9][0-9]{0,19}', key) for key in values['saved']):
        raise ValueError('关注商品编号无效')
    bounds = {}
    try:
        for key in ('since', 'until'):
            if values[key]:
                boundary = datetime.combine(date.fromisoformat(values[key]), time.min)
                bounds[key] = boundary + timedelta(days=1) if key == 'until' else boundary
    except (ValueError, OverflowError):
        raise ValueError('日期须为有效的年-月-日') from None
    if values['since'] and values['until'] and values['since'] > values['until']:
        raise ValueError('开始日期不能晚于结束日期')
    return values, bounds


def _decimal(value):
    """读取非负有限数值；参数：来源值；返回：Decimal或空，不补零。"""
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def _shop_key(row):
    """识别公开店铺路由；参数：观察；返回：可靠路由键或名称近似键及依据。"""
    payload = row.get('payload') or {}
    raw_url = payload.get('shop_url') if isinstance(payload, dict) else None
    if isinstance(raw_url, str):
        try:
            url = urlsplit(raw_url if not raw_url.startswith('//') else 'https:' + raw_url)
            host = (url.hostname or '').lower()
            if url.scheme in {'http', 'https'} and any(host == domain or host.endswith('.' + domain) for domain in ('taobao.com', 'tmall.com')):
                query = parse_qs(url.query)
                identifier = next((query[key][0] for key in ('shop_id', 'shopId', 'user_number_id', 'user_id')
                                   if key in query and re.fullmatch(r'[0-9]{1,25}', query[key][0])), None)
                if identifier:
                    kind = next(key for key in ('shop_id', 'shopId', 'user_number_id', 'user_id') if key in query and query[key][0] == identifier)
                    return host + ':' + kind + ':' + identifier, '店铺公开路由'
                if re.fullmatch(r'shop[0-9]+\.(?:taobao|tmall)\.com', host):
                    return host, '店铺公开路由'
        except ValueError:
            pass
    return ('name:' + row['shop_name'], '按店铺名称近似分组') if row.get('shop_name') else (None, '店铺未记录')


def _percent(count, total):
    """计算展示占比；参数：分子分母；返回：百分数，空分母为零。"""
    return round(count * 100 / total, 1) if total else 0


def analyze_sample(sample, filters):
    """分析已读取的完整样本；参数：真实样本和已校验筛选；返回：统计、分组及筛选后的明细。"""
    batches = {row['id']: row for row in sample['batches']}
    observed = sorted(sample['observations'], key=lambda row: (
        batches[row['batch_id']]['captured_at'] or datetime.min, row['batch_id'],
        row['id'] if sample['snapshot'] == 'range' else row.get('slot_index') or 0, row['id']))
    chosen, occurrences = {}, Counter()
    for source in observed:
        key = 'p:' + str(source['product_id']) if source.get('product_id') is not None else 'o:' + str(source['id'])
        occurrences[key] += 1
        if key not in chosen or sample['snapshot'] == 'range':
            chosen[key] = {**source, 'key': key}
    rows = []
    for source in chosen.values():
        row, batch = dict(source), batches[source['batch_id']]
        row.update({name: batch.get(name) for name in ('task_id', 'captured_at', 'page_number', 'sort_label')})
        row['occurrences'] = occurrences[row['key']]
        row['price'] = _decimal(row.get('price_value'))
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        currency = payload.get('currency_symbol')
        row['price_usable'] = row['price'] is not None and currency in (None, '', '¥', '￥', '元', 'CNY', 'RMB')
        number = row.get('sales_count')
        row['quantity_usable'] = (row.get('sales_parse_status') == 'parsed' and type(number) is int and number >= 0
            and row.get('sales_precision') in {'exact', 'lower_bound', 'approximate'})
        row['sales_group'] = (row.get('sales_metric') or 'unknown') + ':' + (row.get('sales_period') or 'unknown')
        row['sales_group_label'] = METRICS.get(row.get('sales_metric'), '未知口径') + ' · ' + PERIODS.get(row.get('sales_period'), '未知周期')
        row['precision_label'] = PRECISIONS.get(row.get('sales_precision'), '未知')
        row['shop_key'], row['shop_basis'] = _shop_key(row)
        rows.append(row)
    raw_count = len(rows)
    excluded = [row for row in rows if row.get('ad_status') == 'identified'] if filters['exclude_ads'] else []
    rows = [row for row in rows if not filters['exclude_ads'] or row.get('ad_status') != 'identified']
    prices = sorted(row['price'] for row in rows if row['price_usable'])
    distribution = price_distribution(prices)
    price_bins = distribution['bins']
    for row in rows:
        row['price_bin'] = next((item['index'] for item in price_bins if contains_price(row['price'], item)), None) if row['price_usable'] else None
    scale_source = {key: filters[key] for key in ('since', 'until', 'exclude_ads')}
    scale_source.update(keyword=sample['keyword'], snapshot=sample['snapshot'], prices=prices)
    scale_source['bins'] = [{key: item[key] for key in ('low','high','include_low','include_high','kind')} for item in price_bins]
    scale = hashlib.sha256(json.dumps(scale_source, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:16]
    price_filter_reset = filters['bin'] is not None and filters['price_scale'] != scale
    filters = {**filters, 'bin': None if price_filter_reset else filters['bin'], 'price_scale': scale}
    if price_filter_reset:
        filters['quantity_tier'] = ''
    if filters['bin'] is not None and filters['bin'] >= len(price_bins):
        raise ValueError('价格段不属于当前样本')
    peak_count = max((item['count'] for item in price_bins), default=0)
    peak_bins = [item for item in price_bins if item['count'] == peak_count] if peak_count else []
    groups = defaultdict(list)
    for row in rows:
        if row['quantity_usable']:
            groups[row['sales_group']].append(row)
    sales_groups = [{'key': key, 'label': members[0]['sales_group_label'], 'count': len(members)}
                    for key, members in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0]))]
    # 旧链接中的未知收货周期仅在该组已不存在、近30天组确实存在时更新，保留其他真实口径。
    period_filter_updated = False
    if 'received_people:unknown' not in groups and 'received_people:last_30_days' in groups:
        filters = dict(filters)
        for key in ('group', 'table_group'):
            if filters[key] == 'received_people:unknown':
                filters[key] = 'received_people:last_30_days'
                period_filter_updated = True
    selected_group = filters['group'] or (sales_groups[0]['key'] if sales_groups else '')
    if selected_group and selected_group not in {m + ':' + p for m in METRICS for p in PERIODS}:
        raise ValueError('展示数量口径无效')
    filters = {**filters, 'keyword': sample['keyword'], 'snapshot': sample['snapshot'], 'group': selected_group}
    comparable = groups.get(selected_group, [])
    price_cross = []
    for item in price_bins:
        members = [row for row in comparable if row['price_bin'] == item['index']]
        # 近似值不用于判断是否达到精确门槛；下界仅在本身达到门槛时纳入。
        countable = [row for row in members if row['sales_precision'] == 'exact'
                     or row['sales_precision'] == 'lower_bound' and row['sales_count'] >= 10000]
        high = sum(row['sales_count'] >= 10000 for row in countable)
        price_cross.append({**item, 'comparable': len(members), 'threshold_eligible': len(countable),
                            'high': high, 'high_percent': _percent(high, len(countable))})
    scatter = [row for row in comparable if row['price_usable']]
    stats = {'products': len(rows), 'raw_products': raw_count, 'observations': sum(row['occurrences'] for row in rows),
        'source_observations': len(observed), 'unknown_identities': sum(row.get('product_id') is None for row in rows),
        'excluded_ads': len(excluded), 'identified_ads': sum(row.get('ad_status') == 'identified' for row in rows),
        'shops': len({row['shop_key'] for row in rows if row['shop_key']}),
        'missing_shops': sum(not row['shop_key'] for row in rows), 'prices': len(prices),
        'price_min': min(prices) if prices else None, 'price_max': max(prices) if prices else None,
        'price_median': quantile(prices, Decimal('.5')), 'price_q1': quantile(prices, Decimal('.25')),
        'price_q3': quantile(prices, Decimal('.75')), 'quantities': sum(row['quantity_usable'] for row in rows),
        'comparable': len(comparable), 'scatter': len(scatter),
        'high': sum(row['sales_precision'] in {'exact', 'lower_bound'} and row['sales_count'] >= 10000 for row in comparable),
        'sorts': list(dict.fromkeys(batch.get('sort_label') or '排序未记录' for batch in sample['batches'])),
        'pages': sorted({batch['page_number'] for batch in sample['batches'] if batch.get('page_number') is not None}),
        'batch_count': len(sample['batches'])}
    detail = filter_detail(rows, filters)
    shops = group_shops(detail)
    table = shops if filters['view'] == 'stores' else detail
    info = pagination(len(table), filters['page'])
    return {'filters': filters, 'stats': stats,
        'price_bins': price_bins, 'price_cross': price_cross, 'sales_groups': sales_groups,
        'price_distribution': distribution, 'peak_bins': peak_bins, 'price_filter_reset': price_filter_reset,
        'period_filter_updated': period_filter_updated,
        'price_composition': price_composition(rows,price_bins,selected_group),
        'selected_quantity_tier': parse_quantity_tier(filters['quantity_tier']),
        'group_label': next((item['label'] for item in sales_groups if item['key'] == selected_group),
            METRICS.get(selected_group.split(':')[0], '暂无可用数量') + ' · ' + PERIODS.get(selected_group.split(':')[-1], '未知周期') if selected_group else '暂无可用数量'),
        'group_available': selected_group in groups,
        'table_group_available': filters['table_group'] in {'all', 'missing'} or filters['table_group'] in groups,
        'scatter': scatter[:1000], 'scatter_total': len(scatter), 'all_rows': rows, 'detail_rows': detail,
        'table_rows': table[(filters['page'] - 1) * PAGE_SIZE:filters['page'] * PAGE_SIZE],
        'shops': shops, 'pagination': info, 'keywords': sample['keywords'], 'snapshots': sample['snapshots'],
        'batches': sample['batches']}


def filter_detail(rows, filters):
    """筛选商品明细，不改变全局图表范围；参数：样本与条件；返回：排序后的明细。"""
    result = []
    tier = parse_quantity_tier(filters.get('quantity_tier',''))
    for row in rows:
        if filters['view'] == 'saved' and row['key'] not in filters['saved']:
            continue
        if filters['bin'] is not None and row['price_bin'] != filters['bin']:
            continue
        if tier and not (row['price_usable'] and row['quantity_usable'] and row['sales_group']==tier['group']
                         and row['sales_count']==tier['value'] and row['sales_precision']==tier['precision']):
            continue
        if filters['finding'] == 'high' and not (row['quantity_usable'] and row['sales_group'] == filters['group']
                and row['sales_precision'] in {'exact', 'lower_bound'} and row['sales_count'] >= 10000):
            continue
        if filters['store'] and row['shop_key'] != filters['store']:
            continue
        if filters['table_group'] == 'missing' and row['quantity_usable']:
            continue
        if filters['table_group'] not in {'all', 'missing'} and not (row['quantity_usable'] and row['sales_group'] == filters['table_group']):
            continue
        if filters['q'] and filters['q'].casefold() not in ' '.join(str(row.get(key) or '') for key in ('title', 'shop_name', 'platform_product_id')).casefold():
            continue
        result.append(row)
    if filters['sort'].startswith('price_'):
        available = [row for row in result if row['price_usable']]
        missing = [row for row in result if not row['price_usable']]
        return sorted(available, key=lambda row: (row['price'], row['id']), reverse=filters['sort'] == 'price_desc') + missing
    return sorted(result, key=lambda row: (row.get('page_number') is None, row.get('page_number') or 0,
        row.get('slot_index') is None, row.get('slot_index') or 0, row['id']))


def group_shops(rows):
    """汇总当前明细中的店铺；参数：观察行；返回：店铺分组，不推算市场份额。"""
    groups = defaultdict(list)
    for row in rows:
        if row['shop_key']:
            groups[row['shop_key']].append(row)
    result = []
    for key, members in groups.items():
        prices = [row['price'] for row in members if row['price_usable']]
        result.append({'key': key, 'name': members[0]['shop_name'], 'basis': members[0]['shop_basis'],
            'count': len(members), 'percent': _percent(len(members), len(rows)),
            'price_min': min(prices) if prices else None, 'price_max': max(prices) if prices else None})
    return sorted(result, key=lambda row: (-row['count'], row['key']))


def analysis_page(settings, **options):
    """读取并分析搜索词；参数：配置及筛选；返回：可供CLI/Web复用的只读分析结果。"""
    if settings.collection_backend != 'mysql':
        raise ValueError('采集数据库尚未启用')
    filters, bounds = parse_filters(options)
    sample = load_search_sample(settings, keyword=filters['keyword'], snapshot=filters['snapshot'], **bounds)
    return analyze_sample(sample, filters)
