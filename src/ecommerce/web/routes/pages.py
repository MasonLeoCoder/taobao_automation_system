"""页面路由与只读状态接口。"""
from flask import Blueprint, abort, current_app, jsonify, render_template,request
from ecommerce.modules.monitoring.service import overview
from ecommerce.shared.features import FEATURES

pages = Blueprint("pages", __name__)


@pages.get("/")
def home():
    """展示工作台；参数：无；返回：首页响应。"""
    return render_template("dashboard.html", active="home", **overview(current_app.config["SETTINGS"]))


@pages.get("/tasks")
def tasks():
    """展示真实任务记录；参数：无；返回：任务页面。"""
    return render_template("tasks.html", active="tasks", **overview(current_app.config["SETTINGS"]))


@pages.get("/modules/<key>")
def module(key: str):
    """展示能力说明；参数：模块标识；返回：模块页面或 404。"""
    feature = next((item for item in FEATURES if item["key"] == key), None)
    if feature is None:
        abort(404)
    return render_template("module.html", active=key, feature=feature, **overview(current_app.config["SETTINGS"]))


@pages.get("/api/status")
def status():
    """返回运行状态；参数：无；返回：JSON 响应。"""
    return jsonify(overview(current_app.config["SETTINGS"]))


@pages.get("/health")
def health():
    """返回服务健康状态；参数：无；返回：JSON 响应。"""
    return jsonify(status="ok")


def event_query():
    """校验日志筛选参数；参数：当前请求；返回：统一查询结果。"""
    settings=current_app.config['SETTINGS']
    if settings.task_backend!='mysql':
        return {'events':[],'next_cursor':0,'message':'当前任务源未启用 MySQL 日志'}
    try:
        return settings.task_database.list_events(task_id=request.args.get('task_id') or None,
            after=int(request.args.get('after','0')),limit=int(request.args.get('limit','100')),
            level=request.args.get('level') or None,since=request.args.get('since') or None,until=request.args.get('until') or None)
    except (ValueError,TypeError):
        abort(400,description='日志筛选参数无效；时间须包含时区，数量为 1–500，任务编号为32位')


@pages.get('/api/events')
def events():
    """查询历史事件；参数：任务、级别、带时区时间及游标；返回：JSON。"""
    return jsonify(event_query())


@pages.get('/logs')
def logs():
    """显示分页历史日志；参数：同事件接口；返回：安全转义页面。"""
    return render_template('logs.html',active='logs',features=FEATURES,
                           shop_name=current_app.config['SETTINGS'].shop_name,**event_query())


def collection_query(kind,batch_id=None,*,after_override=None):
    """解析采集筛选并调用统一业务查询；参数：种类及可选内部批次；返回：结果。"""
    from ecommerce.modules.collection.queries import query
    settings=current_app.config['SETTINGS']
    if settings.collection_backend!='mysql':
        abort(409,description='采集数据库尚未启用')
    try:
        parameters={'after':int(request.args.get('after','0')),'limit':int(request.args.get('limit','50'))}
        if after_override is not None:
            parameters['after']=int(after_override)
        if kind=='products':
            parameters['keyword']=request.args.get('keyword') or None
        else:
            isolated=request.args.get('include_quarantined','0')
            if isolated not in {'0','1'}:
                raise ValueError('隔离开关无效')
            parameters['include_quarantined']=isolated=='1'
            if kind=='batches':
                parameters.update(product_id=request.args.get('product_id') or None,module=request.args.get('module') or None)
            else:
                parameters.update(batch_id=batch_id,kind=request.args.get('kind') or None,
                    question_id=int(request.args['question_id']) if request.args.get('question_id') else None)
        return query(settings,kind,**parameters)
    except (ValueError,TypeError):
        abort(400,description='采集查询参数无效，数量为 1–200，编号和游标须为有效数字')
    except LookupError:
        abort(404,description='批次不存在或尚未发布')


@pages.get('/api/collections/products')
def collection_products():
    """查询有正式来源的商品；参数：关键词及分页；返回：JSON。"""
    return jsonify(collection_query('products'))


@pages.get('/api/collections/batches')
def collection_batches():
    """查询批次与范围；参数：商品、模块及分页；返回：JSON。"""
    return jsonify(collection_query('batches'))


@pages.get('/api/collections/batches/<int:batch_id>/records')
def collection_records(batch_id):
    """分页读取批次观察；参数：内部批次和明细筛选；返回：JSON。"""
    return jsonify(collection_query('records',batch_id))


@pages.get('/collections')
def collections():
    """显示商品来源与实际批次；参数：当前筛选；返回：页面。"""
    settings=current_app.config['SETTINGS']
    data={'products':[],'batches':[],'next_cursor':0}
    if settings.collection_backend=='mysql':
        product_data=collection_query('products',after_override=request.args.get('product_after','0'))
        data={**product_data,**collection_query('batches'),'product_next_cursor':product_data['next_cursor']}
    return render_template('collections.html',active='collections',features=FEATURES,shop_name=settings.shop_name,
                           enabled=settings.collection_backend=='mysql',**data)


@pages.get('/collections/batches/<int:batch_id>')
def collection_batch(batch_id):
    """显示批次范围和已采明细；参数：内部批次与分页；返回：页面。"""
    settings=current_app.config['SETTINGS']
    data=collection_query('records',batch_id)
    if data['kind']=='media':
        data['records']=[row for row in data['records'] if row['role'] in {'main_image','detail_image','main_video','sku_image'}]
    return render_template('collection_batch.html',active='collections',features=FEATURES,shop_name=settings.shop_name,**data)
