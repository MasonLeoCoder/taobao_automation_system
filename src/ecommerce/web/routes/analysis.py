"""搜索词分析页面及只读CSV导出；统计规则由分析业务模块统一提供。"""
import csv
from io import StringIO
from decimal import Decimal
from urllib.parse import urlencode

from flask import Blueprint, abort, current_app, render_template, request, url_for, Response, redirect

from ecommerce.modules.analysis.service import analysis_page
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
    if data['filters']['view'] != 'stores':
        from ecommerce.infrastructure.storage.search_analysis import attach_previews
        from ecommerce.modules.collection.queries import inspect_thumbnails
        attach_previews(settings, data['table_rows'])
        inspect_thumbnails(settings, data['table_rows'])
    values = _parameters(data['filters'])

    def analysis_url(**overrides):
        """保留分析范围并回到首页；参数：覆盖条件；返回：站内分析地址。"""
        if 'quantity_tier' not in overrides and ('bin' in overrides or 'finding' in overrides):
            overrides['quantity_tier'] = None
        return url_for('analysis.index', **{**values, 'page': 1, **overrides})

    browser_data = {'points': [_point(row) for row in data['scatter']],
        'details': [_point(row) for row in data['table_rows']] if data['filters']['view'] != 'stores' else [],
        'price_bins': data['price_bins'], 'filters': data['filters']}
    return render_template('search_analysis.html', active='analysis', shop_name=settings.shop_name,
        analysis_url=analysis_url, analysis_money=_money, export_url=url_for('analysis.export', **{**values, 'page': 1}),
        browser_data=public(browser_data), **public({key: value for key, value in data.items() if key not in {'all_rows', 'detail_rows', 'scatter'}}))


def _csv_cell(value):
    """保护导出文本；参数：单元格；返回：阻止公式执行的文本，保留长编号。"""
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')) else text


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
           if data['selected_quantity_tier'] else []))
    if filters['view'] == 'stores':
        writer.writerow(['店铺名称', '识别依据', '样本商品数', '占当前明细商品比例（%）', '最低入口价', '最高入口价'])
        for row in data['shops']:
            writer.writerow([_csv_cell(row['name']), row['basis'], row['count'], row['percent'], row['price_min'], row['price_max']])
    else:
        writer.writerow(['观察编号', '商品内部编号', '平台商品编号（文本）', '标题', '入口价数值', '价格原文', '价格条件',
            '数量原文', '数量数值', '数量口径及周期', '数量精度', '店铺', '店铺识别依据', '地区', '广告识别', '采集时间（北京时间）',
            '任务编号', '批次编号', '搜索页码', '原始格子位置', '范围内展示次数'])
        for row in data['detail_rows']:
            writer.writerow([_csv_cell(value) for value in [row['id'], row['product_id'],
                "'" + row['platform_product_id'] if row.get('platform_product_id') else '', row.get('title'), row['price'],
                row.get('price_text'), row.get('price_label'), row.get('sales_text'), row.get('sales_count'), row['sales_group_label'],
                row['precision_label'], row.get('shop_name'), row['shop_basis'], row.get('location_text'),
                {'identified': '已识别广告', 'not_identified': '未命中标识（不等于非广告）'}.get(row.get('ad_status'), '广告未确认'),
                row.get('captured_at'), row.get('task_id'), row['batch_id'], row.get('page_number'), row.get('slot_index'), row['occurrences']]])
    response = Response('\ufeff' + buffer.getvalue(), content_type='text/csv; charset=utf-8')
    response.headers['Content-Disposition'] = 'attachment; filename="search-analysis-' + datetime.now(BEIJING).strftime('%Y%m%d-%H%M%S-%f') + '.csv"'
    return response
