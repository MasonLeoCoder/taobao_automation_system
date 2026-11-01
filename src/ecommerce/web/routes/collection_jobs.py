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
    if state.get('mode')=='detail' and not any(item.get('batch_id') or item.get('valid') for item in state.get('results',[])):
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
    for item in state.get('failures',[]):
        item['product_url']=url_for('pages.collection_product',product_id=item['product_id'])
        item['logs_url']=url_for('pages.logs',task_id=item.get('task_id') or state['id'])
    return state


@collection_jobs.get('/api/collection-jobs/<identifier>/recovery')
def recovery_preview(identifier):
    """只读恢复位置和清理范围；参数：原任务；返回：带版本的预览。"""
    from ecommerce.modules.collection import dispatch
    configured_job(identifier)
    return jsonify(dispatch.preview(current_app.config['SETTINGS'],identifier))


@collection_jobs.post('/api/collection-jobs/<identifier>/resume')
def resume_job(identifier):
    """按用户看到的版本恢复，执行时再次核对；参数：原任务及版本；返回：新执行。"""
    from ecommerce.modules.collection import dispatch
    body=request.get_json()
    if not isinstance(body,dict) or set(body)!={'revision'} or type(body['revision']) is not int:
        raise ValueError('请先查看恢复计划并提交对应版本')
    arguments={'submission_key':request.headers['X-Submission-Id']} if request.headers.get('X-Submission-Id') else {}
    return jsonify(present(dispatch.resume(current_app.config['SETTINGS'],identifier,body['revision'],**arguments))),202


@collection_jobs.post('/api/collection-jobs/<identifier>/retry')
def retry_module(identifier):
    """手动排队补采失败模块；参数：来源任务、商品和模块；返回：队列编号。"""
    from ecommerce.modules.collection import dispatch
    body=request.get_json()
    if not isinstance(body,dict) or set(body)!={'product_id','module'}:
        raise ValueError('请指定原任务对应的商品和模块')
    return jsonify(dispatch.retry(current_app.config['SETTINGS'],identifier,body['product_id'],body['module'])),202


@collection_jobs.route('/api/collection-queue',methods=['GET','POST'])
def retry_queue():
    """查看或明确暂停/恢复队列；参数：可选动作；返回：队列。"""
    from ecommerce.modules.collection import dispatch
    settings=current_app.config['SETTINGS']
    if settings.task_backend!='mysql' or settings.collection_backend!='mysql':
        if request.method=='GET':return jsonify(items=[])
        raise ValueError('补采队列需要启用MySQL任务和采集存储')
    if request.method=='GET':
        from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
        from ecommerce.infrastructure.tasks.ownership import execution_key
        return jsonify(items=dispatch.queue(settings),**CollectionJobStore(settings).queue_state(execution_key(settings.for_platform('taobao'))))
    body=request.get_json()
    if not isinstance(body,dict) or set(body)!={'action'}:
        raise ValueError('请指定队列动作')
    return jsonify(items=dispatch.control_queue(settings,body['action']))


def configured_job(identifier):
    """读取当前执行配置内的任务；参数：派发编号；返回：经归属核验的派发。"""
    settings=current_app.config['SETTINGS']
    return service.get_job(settings,identifier)


@collection_jobs.get('/api/collection-jobs')
def history():
    """分页读取当前资源采集申请；参数：page/status；返回：公开状态及分页信息。"""
    import re
    page=request.args.get('page','1')
    if not re.fullmatch('[1-9][0-9]{0,9}',page) or any(len(request.args.getlist(key))>1 for key in ('page','status')):
        raise ValueError('采集任务页码或状态无效')
    result=service.history(current_app.config['SETTINGS'],int(page),request.args.get('status','all'))
    result['jobs']=[present(state) for state in result['jobs']]
    return jsonify(result)


@collection_jobs.post('/api/collection-jobs')
def create():
    """派发关键词或任意商品采集；参数：JSON配置；返回：202任务描述。"""
    return jsonify(present(service.start_configured(current_app.config['SETTINGS'],request.get_json(),
        submission_key=request.headers.get('X-Submission-Id')))),202


@collection_jobs.get('/api/collection-jobs/<identifier>')
def job_status(identifier):
    """查询本执行配置的任务；参数：编号；返回：公开状态。"""
    return jsonify(present(service.public_status(current_app.config['SETTINGS'],configured_job(identifier))))


@collection_jobs.post('/api/collection-jobs/<identifier>/control')
def job_control(identifier):
    """将人工反馈送至指定任务；参数：编号、指令；返回：接收说明。"""
    return control(None,identifier)


@collection_jobs.post('/api/collection-jobs/<identifier>/start')
def start_job(identifier):
    """只启动已保存的选定申请，参数不能覆盖原范围；参数：编号及空对象；返回：公开状态。"""
    if request.get_json()!={}:
        raise ValueError('开始本次采集只接受已保存的申请，不能修改参数')
    from ecommerce.modules.collection import dispatch
    return jsonify(present(dispatch.start_job(current_app.config['SETTINGS'],identifier))),202


@collection_jobs.get('/api/collection-jobs/active')
def active():
    """恢复全站正在进行的网页采集；参数：无；返回：公开任务列表。"""
    return jsonify(jobs=[present(state) for state in service.active(current_app.config['SETTINGS'])])


@collection_jobs.get('/api/collections/products/<product_id>/jobs')
def recent(product_id):
    """恢复当前商品模块状态；参数：商品；返回：最近任务。"""
    settings=current_app.config['SETTINGS']
    states=service.recent(settings,product_id)
    with_sku_update=request.args.get('sku_update')=='1'
    with_module_updates=request.args.get('module_updates')=='1'
    if with_sku_update or with_module_updates:
        known={job['id'] for job in states}
        states+= [job for job in service.active(settings) if job['product_id']==product_id and job['id'] not in known]
    extra=service.product_status(settings,product_id,with_overview=request.args.get('overview')=='1',states=states,
        **({'with_sku_update':True} if with_sku_update else {}),
        **({'with_module_updates':True} if with_module_updates else {}))
    if 'overview' in extra:
        from ecommerce.web.collection_overview import present as overview_present
        extra['overview']=overview_present(extra['overview'],product_id)
    if 'sku_update' in extra:
        from ecommerce.web.sku_update import present as sku_present
        extra['sku_update']=sku_present(extra['sku_update'],product_id)
    if 'module_updates' in extra:
        from ecommerce.web.module_update import present as update_present
        extra['module_updates']={key:update_present(value,product_id,key) for key,value in extra['module_updates'].items()}
    return jsonify(jobs=[present(state) for state in states],**extra)


@collection_jobs.get('/api/collections/products/<product_id>/module-update/<module>')
def check_update_submission(product_id,module):
    """提交超时后只读核查原编号；参数：商品、模块和提交编号；返回：原申请或空。"""
    from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore, identifier
    from ecommerce.infrastructure.tasks.ownership import execution_key
    settings=current_app.config['SETTINGS']
    service.validate(settings,product_id,module)
    key=identifier(request.args.get('submission_id',''))
    row=CollectionJobStore(settings).submitted(execution_key(settings.for_platform('taobao')),key)
    if row and (row['product_id']!=product_id or row['module']!=module):
        raise ValueError('提交编号与当前商品模块不匹配')
    return jsonify(job=present(service.public_status(settings,row)) if row else None)


@collection_jobs.post('/api/collections/products/<product_id>/module-update/<module>')
def update_module(product_id,module):
    """唯一模块确认更新；参数：模块和确认范围；返回：新申请或原在途申请。"""
    body=request.get_json()
    if not isinstance(body,dict) or set(body)-{'confirm','unlimited'} or body.get('confirm') is not True:
        raise ValueError('请明确确认更新范围')
    return jsonify(present(service.start_module_update(current_app.config['SETTINGS'],product_id,module,
        unlimited=body.get('unlimited',False),submission_key=request.headers.get('X-Submission-Id')))),202


@collection_jobs.post('/api/collections/products/<product_id>/sku-update')
def update_skus(product_id):
    """明确确认后只采集本商品全部SKU；参数：商品及confirm=true；返回：新申请或已有在途申请。"""
    body=request.get_json()
    if not isinstance(body,dict) or set(body)!={'confirm'} or body['confirm'] is not True:
        raise ValueError('请确认更新本商品全部SKU信息')
    return jsonify(present(service.start_sku_update(current_app.config['SETTINGS'],product_id,
        submission_key=request.headers.get('X-Submission-Id')))),202


@collection_jobs.post('/api/collections/products/<product_id>/jobs')
def start(product_id):
    """派发单模块或补齐剩余模块；参数：商品及 JSON 请求；返回：202任务描述。"""
    body=request.get_json()
    if isinstance(body,dict) and body=={'mode':'remaining'}:
        state=service.start_remaining(current_app.config['SETTINGS'],product_id,submission_key=request.headers.get('X-Submission-Id'))
    elif isinstance(body,dict) and set(body)=={'module'} and isinstance(body['module'],str):
        state=service.start(current_app.config['SETTINGS'],product_id,body['module'],submission_key=request.headers.get('X-Submission-Id'))
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
