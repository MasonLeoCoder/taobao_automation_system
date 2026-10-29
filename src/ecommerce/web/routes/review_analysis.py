"""商品评价分析页面与明确计费操作；GET和导出只读取MySQL。"""
import json
import secrets
from urllib.parse import urlsplit
from flask import Blueprint, abort, current_app, jsonify, render_template, request, Response, redirect, url_for
from ecommerce.modules.review_analysis import service, rules
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.time_values import iso_value
from ecommerce.shared.pagination import pagination, PAGE_SIZE
from ecommerce.infrastructure.ai.client import AIError
from ecommerce.shared.exceptions import TaskBusy

review_analysis = Blueprint('review_analysis', __name__)


def settings():
    """取得本应用配置；参数：无；返回：配置。"""
    return current_app.config['SETTINGS']


@review_analysis.before_request
def protect():
    """保护本机写操作；参数：请求；返回：无或明确拒绝。"""
    if request.method != 'POST':
        return
    if urlsplit(request.host_url).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        abort(403)
    if not secrets.compare_digest(request.headers.get('X-Collection-Token', ''), current_app.config['COLLECTION_TOKEN']):
        abort(403)
    if request.headers.get('Origin') and request.headers['Origin'].rstrip('/') != request.host_url.rstrip('/'):
        abort(403)
    if not request.is_json:
        abort(415)


def payload(allowed, required=()):
    """校验请求对象与字段；参数：允许键集合；返回：JSON对象。"""
    values = request.get_json()
    if not isinstance(values, dict) or set(values) - set(allowed) or set(required) - set(values):
        raise ValueError('请求参数无效。')
    return values


@review_analysis.errorhandler(ValueError)
@review_analysis.errorhandler(LookupError)
@review_analysis.errorhandler(AIError)
@review_analysis.errorhandler(TaskBusy)
def invalid(error):
    """展示安全业务错误；参数：异常；返回：明确错误页或JSON。"""
    if request.method == 'POST':
        return jsonify(error=str(error)), 409 if isinstance(error, TaskBusy) else 400
    if request.args.get('dialog') == '1':
        return Response(render_template('review_analysis_dialog_error.html', message=str(error)),
            status=404 if isinstance(error, LookupError) else 400, headers={'X-Review-Analysis-Fragment':'1'})
    return render_template('review_analysis_error.html', active='collections', shop_name=settings().shop_name, message=str(error)), 404 if isinstance(error, LookupError) else 400


@review_analysis.get('/analysis/reviews/products/<product>')
def index(product):
    """商品分析历史；参数：平台商品编号；返回：最近任务。"""
    raw = request.args.get('batch')
    try:
        batch = 'all' if raw == 'all' else int(raw) if raw else None
    except ValueError:
        raise ValueError('批次编号无效。') from None
    data = service.history_page(settings(), product, batch)
    return render_template('review_analysis_history.html', active='collections', shop_name=settings().shop_name,
                           product=product, **data)


@review_analysis.get('/api/analysis/reviews/products/<product>/summary')
def batch_summary(product):
    """读取当前已加载批次的状态，不跟随下拉框未应用值；参数：商品、批次；返回：无配置的摘要。"""
    try:
        batch = int(request.args.get('batch', ''))
    except ValueError:
        raise ValueError('请指定当前列表的评价批次。') from None
    data = service.history_page(settings(), product, batch)
    latest = data['latest_task']
    return jsonify(batch_id=batch, active_id=data['active_task']['id'] if data['active_task'] else None,
        result_id=data['latest_result']['id'] if data['latest_result'] else None,
        latest_status=('interrupted' if latest['appears_interrupted'] else latest['status']) if latest else None)


@review_analysis.get('/analysis/reviews/products/<product>/new')
def new(product):
    """展示确认与调用预算，不联网；参数：商品、可选批次；返回：页面。"""
    raw = request.args.get('batch')
    try:
        batch = int(raw) if raw else None
    except ValueError:
        raise ValueError('批次编号须为整数。') from None
    if batch is None:
        choices = repository.batch_choices(settings(), service.product_id(product))
        if not choices:
            raise LookupError('尚无评价资料，请返回商品评价明细选择资料。')
        return redirect(url_for('review_analysis.new', product=product, batch=choices[0]['id']))
    data = service.preview(settings(), product, batch)
    if request.args.get('dialog') == '1':
        return Response(render_template('review_analysis_form.html', data=data, product=product, dialog_mode=True),
                        headers={'X-Review-Analysis-Fragment':'1'})
    return render_template('review_analysis_new.html', active='collections', shop_name=settings().shop_name, data=data, product=product)


@review_analysis.post('/api/analysis/reviews/products/<product>')
def start(product):
    """确认后提交分析；参数：商品和JSON；返回：任务地址。"""
    values = payload({'batch_id', 'model', 'input_hash', 'plan_hash', 'max_calls', 'max_tokens', 'allow_paid', 'force'},
                     {'batch_id', 'model', 'input_hash'})
    result = service.start(settings(), product, **values)
    return jsonify(**result, url='/analysis/reviews/tasks/' + result['id']), 202


@review_analysis.get('/analysis/reviews/tasks/<task_id>')
def result(task_id):
    """查看已存结果及依据；参数：编号和明细页码；返回：结果页。"""
    data = service.read(settings(), task_id)
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        raise ValueError('页码须为整数。') from None
    info = pagination(len(data['details']), page)
    data['detail_page'] = data['details'][(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    return render_template('review_analysis_result.html', active='collections', shop_name=settings().shop_name,
        data=data, info=info, page=page, names={'positive':'正面', 'negative':'负面', 'mixed':'正负并存', 'neutral':'中性', 'unclear':'待确认'},
        kinds={'specific':'有可归类信息', 'general':'笼统表态', 'unclear':'无法判断', 'empty':'空白'}, task=data['task'])


@review_analysis.get('/api/analysis/reviews/tasks/<task_id>')
def status(task_id):
    """读取进度摘要；参数：任务编号；返回：无原文、无配置的安全状态。"""
    data = service.read(settings(), task_id)
    t = data['task']
    return jsonify({k: t[k] for k in ('id', 'status', 'message', 'source_count', 'processed_count', 'usage', 'appears_interrupted')})


@review_analysis.post('/api/analysis/reviews/tasks/<task_id>/resume')
def resume(task_id):
    """明确恢复未完成部分；参数：费用及未知请求确认；返回：任务编号。"""
    values=payload({'allow_paid', 'acknowledge_unknown', 'max_tokens', 'max_calls','plan_hash'}, {'plan_hash'})
    if not isinstance(values['plan_hash'],str) or len(values['plan_hash'])!=64:
        raise ValueError('请先查看本次补处理计划，再确认继续。')
    return jsonify(service.resume(settings(), task_id, **values))


@review_analysis.post('/api/analysis/reviews/tasks/<task_id>/recover')
def recover(task_id):
    """仅恢复已有返回，不接收费用确认或调用AI；参数：任务；返回：本地恢复结果。"""
    payload(set())
    return jsonify(service.recover_saved(settings(),task_id))


@review_analysis.post('/api/analysis/reviews/tasks/<task_id>/details/<detail_id>')
def correct(task_id, detail_id):
    """人工修订和本地重统计；参数：完整修订、原因和版本；返回：新报告版本。"""
    service.identifier(detail_id)
    fields = {'expected_revision', 'kind', 'points', 'excluded', 'reason'}
    return jsonify(service.correct(settings(), task_id, detail_id, **payload(fields, fields)))


@review_analysis.post('/api/analysis/reviews/tasks/<task_id>/recount')
def recount(task_id):
    """本地重新统计；参数：任务；返回：版本，不调用AI。"""
    payload(set())
    return jsonify(service.recount(settings(), task_id))


@review_analysis.get('/analysis/reviews/tasks/<task_id>/export')
def export(task_id):
    """导出MySQL中保存的完整结果；参数：任务；返回：JSON下载，无新AI请求。"""
    data = repository.read(settings(), service.identifier(task_id))
    if request.args.get('format') == 'html':
        return Response(render_template('review_analysis_export.html', data=data), content_type='text/html; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="review_analysis_{task_id}.html"'})
    if request.args.get('format', 'json') != 'json':
        raise ValueError('导出格式仅支持JSON或HTML。')
    # 任务参数含本机配置路径引用，不包含密钥；下载只保留分析相关设置。
    data['task']['parameters'] = {k: v for k, v in data['task']['parameters'].items() if k != 'config_file'}
    text = json.dumps(data, ensure_ascii=False, indent=2, default=lambda v: iso_value(v))
    return Response(text, content_type='application/json; charset=utf-8', headers={
        'Content-Disposition': f'attachment; filename="review_analysis_{task_id}.json"'})
