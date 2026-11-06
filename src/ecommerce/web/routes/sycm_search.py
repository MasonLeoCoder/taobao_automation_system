"""生意参谋搜索排行采集、历史与 CSV。"""
from csv import writer
from io import StringIO
from secrets import compare_digest
from urllib.parse import urlsplit

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request
from ecommerce.infrastructure.storage import sycm_search as repository
from ecommerce.shared.exceptions import TaskBusy

sycm_search = Blueprint('sycm_search', __name__)


def settings():
    return current_app.config['SETTINGS']


def workbench():
    return current_app.extensions['sycm_search']


@sycm_search.before_request
def protect():
    if settings().collection_backend != 'mysql' or settings().task_backend != 'mysql':
        abort(409, description='搜索排行采集需要启用 MySQL')
    if request.method == 'POST' and (urlsplit(request.host_url).hostname not in {'localhost', '127.0.0.1', '::1'}
            or request.headers.get('Origin', request.host_url).rstrip('/') != request.host_url.rstrip('/')
            or not compare_digest(request.headers.get('X-Collection-Token', ''), current_app.config['COLLECTION_TOKEN'])):
        abort(403)


@sycm_search.errorhandler(ValueError)
@sycm_search.errorhandler(TaskBusy)
def rejected(error):
    return jsonify(error=str(error)), 409 if isinstance(error, TaskBusy) else 422


@sycm_search.errorhandler(LookupError)
def missing(error):
    return jsonify(error=str(error)), 404


def _filters():
    period = request.args.get('period', '7d')
    word_type = request.args.get('type', 'search')
    if period not in repository.PERIODS or word_type not in repository.WORD_TYPES:
        raise ValueError('统计周期或词类型无效')
    return period, word_type


@sycm_search.get('/sycm/search-rank')
def index():
    period, word_type = _filters()
    categories = repository.categories(settings())
    available = [row for row in categories if row['is_visible']]
    default = next((row for row in available if row['name'] == '粘毛器'), available[0] if available else None)
    category_id = request.args.get('category', default['id'] if default else 0, type=int)
    category = next((row for row in categories if row['id'] == category_id), None)
    batch_id = request.args.get('batch') or None
    filter_scope = request.args.get('filter_scope')
    if filter_scope is not None and filter_scope != f'{category_id}:{period}':
        batch_id = None
    view = 'history' if request.args.get('view') == 'history' else 'library'
    data = repository.browse(settings(), category_id, period, batch_id, word_type,
                             request.args.get('page', 1, type=int))
    return render_template('sycm_search.html', active='sycm-search', shop_name=settings().shop_name,
        categories=categories, available=available, category=category, category_id=category_id,
        periods=repository.PERIODS, word_types=repository.WORD_TYPES, period=period, word_type=word_type,
        view=view, explicit_batch=bool(batch_id), **data)


@sycm_search.post('/api/sycm/search-rank/start')
def start():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'category_id','period','pages','word_types','sycm_path'}:
        raise ValueError('请选择类目、周期、词类型和页数')
    category_id, period, pages, types = (data[name] for name in ('category_id','period','pages','word_types'))
    if (type(category_id) is not int or not isinstance(period, str) or period not in repository.PERIODS
            or type(pages) is not int or not 1 <= pages <= 2_147_483_647 or not isinstance(types, list)
            or not 1 <= len(types) <= len(repository.WORD_TYPES)
            or any(not isinstance(value, str) or value not in repository.WORD_TYPES for value in types)
            or len(set(types)) != len(types)):
        raise ValueError('类目、周期、页数或词类型无效')
    category = next((row for row in repository.categories(settings()) if row['id'] == category_id and row['is_visible']), None)
    if not category:
        raise ValueError('请选择当前可见的千牛类目')
    path = data['sycm_path']
    if not isinstance(path, str) or len(path) > 750 or (path.strip() and not all(part.strip() for part in path.split('>'))):
        raise ValueError('生意参谋类目路径无效')
    path = ' > '.join(part.strip() for part in path.split('>')) if path.strip() else category['full_path']
    return jsonify(workbench().start(category, path, period, pages,
        [key for key in repository.WORD_TYPES if key in types])), 202


@sycm_search.get('/api/sycm/search-rank/status')
def status():
    return jsonify(workbench().snapshot())


@sycm_search.post('/api/sycm/search-rank/resume')
def resume():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'prompt'} or not isinstance(data['prompt'], str):
        raise ValueError('人工反馈参数无效')
    workbench().resume(data['prompt'])
    return jsonify(workbench().snapshot())


def _cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) and value != '-':
        return "'" + value
    return value


@sycm_search.get('/sycm/search-rank/export')
def export():
    period, word_type = _filters()
    batch_id = request.args.get('batch')
    if not batch_id:
        raise ValueError('请选择要导出的批次')
    data = repository.browse(settings(), request.args.get('category', 0, type=int), period, batch_id, word_type, export=True)
    batch, rows = data['batch'], data['entries']
    extras = sorted({key for row in rows for key in row['extra_metrics']})
    output = StringIO(newline='')
    csv = writer(output)
    csv.writerow(('批次','统计类目','词类型','榜单','统计周期','开始日期','结束日期','页码','排名','词',
                  '搜索人气','点击率','支付转化率','采集时间', *map(_cell, extras)))
    for row in rows:
        csv.writerow(map(_cell, (batch_id,batch['sycm_category_path'],repository.WORD_TYPES[word_type],'搜索人气',
            repository.PERIODS[period],batch['period_start'],batch['period_end'],row['page_no'],row['rank'],row['keyword'],
            row['popularity_raw'],row['click_rate_raw'],row['payment_conversion_raw'],row['captured_at'],
            *(row['extra_metrics'].get(key) for key in extras))))
    return Response('\ufeff' + output.getvalue(), content_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':'attachment; filename="sycm-search-rank.csv"'})
