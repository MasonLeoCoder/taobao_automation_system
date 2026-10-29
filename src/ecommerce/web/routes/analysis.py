"""搜索词分析页面及只读CSV导出；统计规则由分析业务模块统一提供。"""
import csv
from io import StringIO
from decimal import Decimal
from decimal import ROUND_HALF_UP
from urllib.parse import urlencode

from flask import Blueprint, abort, current_app, render_template, request, url_for, Response, redirect

from ecommerce.modules.analysis.service import analysis_page
from ecommerce.modules.analysis.middle import MIDDLE_FINDINGS
from ecommerce.modules.analysis.shop_concentration import SHOP_FINDINGS
from ecommerce.infrastructure.storage.collection_queries import public

analysis = Blueprint('analysis', __name__)


@analysis.before_request
def remove_retired_filters():
    """清理旧类型链接；参数：当前请求；返回：保留其余条件的跳转，避免隐藏筛选继续生效。"""
    if 'category' in request.args or 'unclassified' in request.args.getlist('finding'):
        values = [(key, value) for key, items in request.args.lists() if key != 'category'
                  for value in items if not (key == 'finding' and value == 'unclassified')]
        return redirect(request.path + ('?' + urlencode(values) if values else ''))


def _read():
    """读取已校验的分析结果；参数：当前请求；返回：业务结果，拒绝无效范围。"""
    settings = current_app.config['SETTINGS']
    if settings.collection_backend != 'mysql':
        abort(409, description='采集数据库尚未启用')
    try:
        return analysis_page(settings, **request.args.to_dict())
    except ValueError as error:
        abort(400, description=str(error))
    except LookupError as error:
        abort(404, description=str(error))


def _parameters(filters):
    """转换业务筛选为链接参数；参数：规范化筛选；返回：可追溯的查询参数。"""
    values = {key: value for key, value in filters.items() if value not in ('', None, [])}
    values['exclude_ads'] = '1' if filters['exclude_ads'] else None
    values['saved'] = ','.join(filters['saved']) or None
    return values


def _money(value):
    """格式化入口价格；参数：数字或空；返回：至少两位小数，保留来源精度或空标记。"""
    if value is None:
        return '—'
    number = Decimal(str(value))
    precision = max(2, -number.normalize().as_tuple().exponent)
    return '¥' + format(number, f'.{precision}f')


def _number(value, places=0):
    """格式化计算依据；参数：整数或Decimal序列化值及位数；返回：最终展示才舍入的数值文本。"""
    if value is None:
        return '—'
    rounded=Decimal(str(value)).quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_UP)
    return format(rounded,f',.{places}f')


def _compact_number(value):
    """简化统计数值；参数：原始统计值；返回：最多两位小数，整数不显示小数点。"""
    text=_number(value,2)
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _stat_money(value):
    """格式化价格统计；参数：未舍入的统计值；返回：最终展示两位小数，不改变商品原价。"""
    return '¥'+_number(value,2) if value is not None else '—'


def _point(row):
    """限制浏览器中的观察字段；参数：分析行；返回：图表及依据侧栏需要的字段。"""
    fields = ('id', 'key', 'product_id', 'platform_product_id', 'title', 'price', 'price_text', 'price_label',
        'sales_text', 'sales_count', 'sales_precision', 'precision_label', 'sales_group_label', 'ad_status',
        'shop_name', 'shop_basis', 'location_text', 'captured_at', 'page_number', 'slot_index', 'batch_id',
        'task_id', 'sort_label', 'occurrences')
    result = {key: row.get(key) for key in fields}
    if result['sales_count'] is not None:
        result['sales_count'] = str(result['sales_count'])
    return result


@analysis.get('/analysis/searches')
def index():
    """展示真实搜索词样本分析；参数：URL筛选；返回：概况、图表和分页明细。"""
    data = _read()
    settings = current_app.config['SETTINGS']
    from ecommerce.infrastructure.storage.search_analysis import attach_previews
    from ecommerce.modules.collection.queries import inspect_thumbnails
    detail_products=data['table_rows'] if data['filters']['view']!='stores' else []
    shop_products=[row for group in data['shop_concentration']['groups'] for row in group['members']]
    unique={row['id']:row for row in [*detail_products,*shop_products]}
    attach_previews(settings,list(unique.values()))
    inspect_thumbnails(settings,list(unique.values()))
    for row in [*detail_products,*shop_products]:
        row.update({key:unique[row['id']][key] for key in ('thumbnail','thumbnail_file','thumbnail_message','source_page') if key in unique[row['id']]})
    values = _parameters(data['filters'])

    def analysis_url(**overrides):
        """保留分析范围并回到首页；参数：覆盖条件；返回：站内分析地址。"""
        if 'region' not in overrides and ('bin' in overrides or 'finding' in overrides):
            overrides.update(region=None,city=None)
        if 'region' in overrides and 'city' not in overrides:
            overrides['city'] = None
        if 'quantity_tier' not in overrides and ('bin' in overrides or 'finding' in overrides):
            overrides['quantity_tier'] = None
        return url_for('analysis.index', **{**values, 'page': 1, **overrides})

    browser_data = {'points': [_point(row) for row in data['scatter']],
        'details': [_point(row) for row in [*detail_products,*shop_products]],
        'price_bins': data['price_bins'], 'filters': data['filters']}
    shop_scope={**values,'page':1,'finding':'shop_all','sort':'concentration','bin':None,'quantity_tier':None,
                'region':None,'city':None,'q':None,'store':None,'table_group':'all','view':'goods'}
    return render_template('search_analysis.html', active='analysis', shop_name=settings.shop_name,
        analysis_url=analysis_url, analysis_money=_money, analysis_number=_number, export_url=url_for('analysis.export', **{**values, 'page': 1}),
        analysis_compact_number=_compact_number, analysis_stat_money=_stat_money,
        middle_export_url=url_for('analysis.export',**{**values,'page':1,'finding':'middle','sort':'concentration',
            'bin':None,'quantity_tier':None,'region':None,'city':None,'q':None,'store':None,'table_group':'all','view':'goods'}),
        shop_items_export_url=url_for('analysis.export',**shop_scope),
        shop_summary_export_url=url_for('analysis.export_shop_summary',**shop_scope),
        browser_data=public(browser_data), **public({key: value for key, value in data.items() if key not in {'all_rows', 'detail_rows', 'scatter'}}))


def _csv_cell(value):
    """保护导出文本；参数：单元格；返回：阻止公式执行的文本，保留长编号。"""
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')) else text


def _middle_metadata(data):
    """保留腰部导出上下文；参数：当前完整分析结果；返回：一行元信息的附加单元格。"""
    if data['filters']['finding'] not in MIDDLE_FINDINGS:
        return []
    m=data['middle']; head=m['head']; waist=m['summary']
    values=['腰部分析口径','同一前50名单取第11～30件，近30天收货人数下界取展示整数',
            '同值排序','页码升序、slot_index升序、采集时间较新优先、观察ID升序',
            '导出对象',{'middle':'腰部商品','middle_shared':'腰部中与头部店铺重合的商品',
                        'middle_boundary10':'第10/11件边界的全部有效同值商品',
                        'middle_boundary30':'第30/31件边界的全部有效同值商品'}[data['filters']['finding']],
            '前50实际件数',data['concentration']['count'],'前50人数计算值合计',data['concentration']['total'],
            '腰部占前50人数计算值比例（%）',m['quantity_share'],
            '腰部店铺分组数',waist['shop_count'],'腰部店铺缺失商品数',waist['missing_shops'],
            '与头部重合店铺数',m['overlap_shop_count'],'重合比例分母（腰部已知店铺数）',waist['shop_count'],
            '腰部最早采集时间',m['earliest'],'腰部最新采集时间',m['latest'],'腰部时间缺失数',m['missing_times']]
    for label,summary in (('头部',head),('腰部',waist)):
        for name,key in (('件数','count'),('人数计算值合计','quantity_total'),('单件平均计算值','quantity_mean'),
                         ('人数计算值中位数','quantity_median'),('有效价格件数','price_count'),('入口价中位数','price_median'),
                         ('入口价Q1','price_q1'),('入口价Q3','price_q3')):
            values.extend([label+name,summary[key]])
    return values


def _shop_metadata(data, force=False):
    """补充店铺计算范围；参数：分析结果、是否为独立汇总导出；返回：CSV附加说明。"""
    if not force and data['filters']['finding'] not in SHOP_FINDINGS:
        return []
    s=data['shop_concentration']
    return ['店铺计算口径','同一前50名单按近30天收货人数展示下限分组，缺店铺单列并排除分母',
            '前50实际商品数',s['selected_count'],'前50人数展示值合计',s['selected_total'],
            '店铺可识别商品数',s['known_count'],'店铺缺失商品数',s['missing_count'],
            '店铺计算分母',s['total'],'缺店铺人数展示值',s['missing_total'],
            '商品覆盖率（%）',s['product_coverage'],'人数展示值覆盖率（%）',s['quantity_coverage'],
            '可靠标识分组数',s['reliable_count'],'名称近似分组数',s['name_count'],
            '店铺CR3（%）',s['metrics'][0]['value'],'店铺CR10（%）',s['metrics'][1]['value'],'店铺HHI',s['hhi'],
            '店铺同值排序','合计降序、最靠前商品计算序号升序、分组键升序',
            '店内占比分母','该店在前50中入选商品的展示值合计']


@analysis.get('/analysis/searches/export.csv')
def export():
    """导出全部已筛选明细；参数：与分析页相同；返回：UTF-8 BOM CSV，不受显示分页截断。"""
    from datetime import datetime
    from ecommerce.shared.time import BEIJING
    data = _read()
    filters = data['filters']
    selected_bin = data['price_bins'][filters['bin']] if filters['bin'] is not None else None
    buffer = StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(['搜索词分析', _csv_cell(filters['keyword']), '采集范围', filters['snapshot'],
        '广告筛选', '排除已识别广告' if filters['exclude_ads'] else '包含已识别广告', '排除数量', data['stats']['excluded_ads'],
        '日期起', filters['since'], '日期止', filters['until'],
        '图表口径', data['group_label'], '明细口径', filters['table_group'], '价格段', selected_bin['label'] if selected_bin else '不限',
        '搜索', _csv_cell(filters['q']), '店铺', _csv_cell(filters['store']), '观察筛选', filters['finding'], '视图', filters['view'],
        '价格边界', selected_bin['boundary_label'] if selected_bin else '全部商品，含价格不可用项',
        '自动分段方式', data['price_distribution']['description'], '价格分段标识', filters['price_scale'],
        '价格筛选状态', '样本或分段变化，已恢复全部价位' if data['price_filter_reset'] else '当前分析范围']
        + (['展示档位筛选',data['selected_quantity_tier']['group_label']+' / '+data['selected_quantity_tier']['label']]
           if data['selected_quantity_tier'] else [])
        + (['发货地区筛选',data['selected_region_label']] if filters['region'] else [])
        + (['集中度筛选',{'top3':'入选样本前3件','top10':'入选样本前10件','top50':'入选样本全部商品'}[filters['finding']],
            '集中度计算口径','近30天收货人数，精确值直接使用，下界值取展示整数',
            '集中度参与件数',data['concentration']['count'],'集中度人数计算值合计',data['concentration']['total']]
           if filters['finding'] in {'top3','top10','top50'} else [])+_middle_metadata(data)+_shop_metadata(data))
    if filters['view'] == 'stores':
        writer.writerow(['店铺名称', '识别依据', '样本商品数', '占当前明细商品比例（%）', '最低入口价', '最高入口价'])
        for row in data['shops']:
            writer.writerow([_csv_cell(row['name']), row['basis'], row['count'], row['percent'], row['price_min'], row['price_max']])
    else:
        writer.writerow(['观察编号', '商品内部编号', '平台商品编号（文本）', '标题', '入口价数值', '价格原文', '价格条件',
            '数量原文', '数量数值', '数量口径及周期', '数量精度', '店铺', '店铺识别依据', '地区', '广告识别', '采集时间（北京时间）',
            '任务编号', '批次编号', '搜索页码', '原始格子位置', '范围内展示次数']
            + (['店铺分组键','店铺计算序号','商品计算序号','店内占比（%）','店铺人数展示值合计'] if filters['finding'] in SHOP_FINDINGS else []))
        shop_items={row['id']:[group['key'],group['rank'],row['rank'],row['shop_share'],group['total']]
                    for group in data['shop_concentration']['groups'] for row in group['members']}
        for row in data['detail_rows']:
            writer.writerow([_csv_cell(value) for value in [row['id'], row['product_id'],
                "'" + row['platform_product_id'] if row.get('platform_product_id') else '', row.get('title'), row['price'],
                row.get('price_text'), row.get('price_label'), row.get('sales_text'), row.get('sales_count'), row['sales_group_label'],
                row['precision_label'], row.get('shop_name'), row['shop_basis'], row.get('location_text'),
                {'identified': '已识别广告', 'not_identified': '未命中标识（不等于非广告）'}.get(row.get('ad_status'), '广告未确认'),
                row.get('captured_at'), row.get('task_id'), row['batch_id'], row.get('page_number'), row.get('slot_index'), row['occurrences']]
                + (shop_items[row['id']] if filters['finding'] in SHOP_FINDINGS else [])])
    response = Response('\ufeff' + buffer.getvalue(), content_type='text/csv; charset=utf-8')
    response.headers['Content-Disposition'] = 'attachment; filename="search-analysis-' + datetime.now(BEIJING).strftime('%Y%m%d-%H%M%S-%f') + '.csv"'
    return response


@analysis.get('/analysis/searches/shop-summary.csv')
def export_shop_summary():
    """导出固定店铺汇总；参数：顶部分析范围；返回：与展开子商品可按分组键核对的CSV。"""
    from datetime import datetime
    from ecommerce.shared.time import BEIJING
    data=_read(); filters=data['filters']
    buffer=StringIO(newline=''); writer=csv.writer(buffer)
    writer.writerow(['店铺集中度汇总','搜索词',_csv_cell(filters['keyword']),'采集范围',filters['snapshot'],
                     '日期起',filters['since'],'日期止',filters['until'],'排除已识别广告',filters['exclude_ads'],
                     '导出范围','固定前50中的已知店铺，明细查询不改变汇总']+_shop_metadata(data,True))
    writer.writerow(['店铺计算序号','店铺分组键','店铺名称','识别依据','入选商品数','人数展示值合计',
                     '样本占比（%）','累计占比（%）','单链接平均值','头部件数','腰部件数','后部件数'])
    for group in data['shop_concentration']['groups']:
        writer.writerow([_csv_cell(group[key]) for key in ('rank','key','name','basis','count','total','share','cumulative',
                                                          'mean','head_count','middle_count','tail_count')])
    response=Response('\ufeff'+buffer.getvalue(),content_type='text/csv; charset=utf-8')
    response.headers['Content-Disposition']='attachment; filename="shop-concentration-'+datetime.now(BEIJING).strftime('%Y%m%d-%H%M%S-%f')+'.csv"'
    return response
