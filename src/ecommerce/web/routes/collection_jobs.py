"""本机关键词、详情及补采的短请求接口；不在请求中执行浏览器操作。"""
import secrets
from urllib.parse import urlsplit

from flask import Blueprint,abort,current_app,jsonify,request,url_for
from ecommerce.modules.collection import web_collection as service
from ecommerce.shared.exceptions import TaskBusy

collection_jobs=Blueprint('collection_jobs',__name__)


@collection_jobs.before_request
def protect_request():
    """限制本机来源和写入令牌；参数：当前请求；返回：无或拒绝请求。"""
    if urlsplit(request.host_url).hostname not in {'127.0.0.1','localhost','::1'}:
        abort(403,description='仅允许本机采集操作')
    if request.method=='POST':
        if not secrets.compare_digest(request.headers.get('X-Collection-Token',''),current_app.config['COLLECTION_TOKEN']):
            abort(403,description='页面操作令牌已失效，请刷新页面后重试')
        origin=request.headers.get('Origin')
        if origin and origin.rstrip('/')!=request.host_url.rstrip('/'):
            abort(403,description='不接受其他网站发起的采集操作')
        if not request.is_json:
            abort(415,description='采集操作需要 JSON 请求')


@collection_jobs.errorhandler(ValueError)
@collection_jobs.errorhandler(TaskBusy)
def invalid_request(error):
    """返回可显示的输入或任务冲突；参数：异常；返回：409 JSON。"""
    return jsonify(error=str(error)),409


@collection_jobs.errorhandler(LookupError)
def missing(error):
    """拒绝商品任务错配；参数：异常；返回：404 JSON。"""
    return jsonify(error=str(error)),404


def present(state):
    """补充站内结果和日志链接；参数：公开状态；返回：页面所需状态。"""
    state['product_url']=url_for('pages.collection_product',product_id=state['product_id']) if state['product_id'] else None
    if state.get('mode')=='detail' and not any(item.get('batch_id') for item in state.get('results',[])):
        state['product_url']=None
    state['result_url']=url_for('pages.collection_product',product_id=state['product_id'],batch=state['batch_id']) if state['batch_id'] and state['product_id'] else None
    state['logs_url']=url_for('pages.logs',task_id=state['id'])
    state['status_url']=url_for('collection_jobs.job_status',identifier=state['id'])
    if state.get('mode') in {'remaining','detail'}:
        state['result_url']=state['product_url']
    if state.get('mode')=='keyword' and (state['batch_id'] or state.get('progress',{}).get('completed_page_count')):
        state['result_url']=url_for('pages.collection_search',task_id=state['id'])
    for item in state.get('results',[]):
        item['result_url']=url_for('pages.collection_product',product_id=state['product_id'],batch=item['batch_id']) if item['batch_id'] else None
    return state


def configured_job(identifier):
    """读取当前执行配置内的任务；参数：派发编号；返回：经归属核验的派发。"""
    settings=current_app.config['SETTINGS']
    return service.get_job(settings,identifier)


@collection_jobs.get('/api/collection-jobs')
def history():
    """恢复配置页最近任务；参数：无；返回：公开状态。"""
    return jsonify(jobs=[present(state) for state in service.history(current_app.config['SETTINGS'])])


@collection_jobs.post('/api/collection-jobs')
def create():
    """派发关键词或任意商品采集；参数：JSON配置；返回：202任务描述。"""
    return jsonify(present(service.start_configured(current_app.config['SETTINGS'],request.get_json()))),202


@collection_jobs.get('/api/collection-jobs/<identifier>')
def job_status(identifier):
    """查询本执行配置的任务；参数：编号；返回：公开状态。"""
    return jsonify(present(service.public_status(current_app.config['SETTINGS'],configured_job(identifier))))


@collection_jobs.post('/api/collection-jobs/<identifier>/control')
def job_control(identifier):
    """将人工反馈送至指定任务；参数：编号、指令；返回：接收说明。"""
    row=configured_job(identifier)
    return control(row['product_id'],identifier)


@collection_jobs.get('/api/collection-jobs/active')
def active():
    """恢复全站正在进行的网页采集；参数：无；返回：公开任务列表。"""
    return jsonify(jobs=[present(state) for state in service.active(current_app.config['SETTINGS'])])


@collection_jobs.get('/api/collections/products/<product_id>/jobs')
def recent(product_id):
    """恢复当前商品模块状态；参数：商品；返回：最近任务。"""
    settings=current_app.config['SETTINGS']
    return jsonify(jobs=[present(state) for state in service.recent(settings,product_id)],plan=service.plan(settings,product_id))


@collection_jobs.post('/api/collections/products/<product_id>/jobs')
def start(product_id):
    """派发单模块或补齐剩余模块；参数：商品及 JSON 请求；返回：202任务描述。"""
    body=request.get_json()
    if isinstance(body,dict) and body=={'mode':'remaining'}:
        state=service.start_remaining(current_app.config['SETTINGS'],product_id)
    elif isinstance(body,dict) and set(body)=={'module'} and isinstance(body['module'],str):
        state=service.start(current_app.config['SETTINGS'],product_id,body['module'])
    else:
        raise ValueError('请指定唯一采集模块或补齐剩余模块模式')
    return jsonify(present(state)),202


@collection_jobs.get('/api/collections/products/<product_id>/jobs/<identifier>')
def status(product_id,identifier):
    """读取指定派发进度；参数：商品、任务；返回：公开状态。"""
    return jsonify(present(service.status(current_app.config['SETTINGS'],product_id,identifier)))


@collection_jobs.post('/api/collections/products/<product_id>/jobs/<identifier>/control')
def control(product_id,identifier):
    """接收针对当前提示的人工决定；参数：商品、任务、指令和提示编号；返回：接收说明。"""
    body=request.get_json()
    if not isinstance(body,dict) or set(body)-{'action','prompt_id'}:
        raise ValueError('人工反馈参数无效')
    return jsonify(message=service.control(current_app.config['SETTINGS'],product_id,identifier,
                                           body.get('action'),body.get('prompt_id')))
