"""页面路由与只读状态接口。"""
from flask import Blueprint, abort, current_app, jsonify, render_template,request, send_file
from ecommerce.modules.monitoring.service import overview, task_history, local_task_alerts
from ecommerce.shared.features import FEATURES

pages = Blueprint("pages", __name__)


@pages.get("/")
def home():
    """展示工作台；参数：无；返回：首页响应。"""
    return render_template("dashboard.html", active="home", **overview(current_app.config["SETTINGS"]))


@pages.get("/tasks")
def tasks():
    """按页展示全部任务；参数：页码；返回：每页20条的任务页面。"""
    try:
        data=task_history(current_app.config['SETTINGS'],integer_arg('page',1),sort_by=request.args.get('sort'),order=request.args.get('order'))
    except ValueError as error:
        abort(400,description=str(error))
    except LookupError:
        abort(404,description='任务页不存在')
    return render_template("tasks.html", active="tasks",pagination=data, **data)


@pages.get("/modules/<key>")
def module(key: str):
    """展示能力说明；参数：模块标识；返回：模块页面或 404。"""
    feature = next((item for item in FEATURES if item["key"] == key), None)
    if feature is None:
        abort(404)
    if key=='collection':
        from ecommerce.modules.collection.web_collection import MODULES
        settings=current_app.config['SETTINGS']
        return render_template('collection_start.html',active=key,features=FEATURES,shop_name=settings.shop_name,
            modules=MODULES,enabled=settings.task_backend=='mysql' and settings.collection_backend=='mysql')
    return render_template("module.html", active=key, feature=feature, **overview(current_app.config["SETTINGS"]))


@pages.get("/api/status")
def status():
    """返回运行状态；参数：无；返回：JSON 响应。"""
    return jsonify(overview(current_app.config["SETTINGS"]))


@pages.get("/health")
def health():
    """返回服务健康状态；参数：无；返回：JSON 响应。"""
    return jsonify(status="ok")


def event_query(time_filters=None,page=None):
    """校验日志筛选参数；参数：当前请求；返回：统一查询结果。"""
    settings=current_app.config['SETTINGS']
    if settings.task_backend!='mysql':
        from ecommerce.shared.pagination import pagination
        return {'events':[],'next_cursor':0,'message':'当前任务源未启用 MySQL 日志',**pagination(0,page or 1)}
    try:
        return settings.task_database.list_events(task_id=request.args.get('task_id') or None,
            after=0 if page is not None else int(request.args.get('after','0')),
            limit=20 if page is not None else int(request.args.get('limit','100')),
            before=int(request.args['before']) if page is None and request.args.get('before') else None,
            page=page,
            include_task_name=time_filters is not None,
            sort_by=request.args.get('sort') if page is not None else None,
            order=request.args.get('order') if page is not None else None,
            level=request.args.get('level') or None,
            since=time_filters['since'] if time_filters is not None else request.args.get('since') or None,
            until=time_filters['until'] if time_filters is not None else request.args.get('until') or None)
    except (ValueError,TypeError):
        abort(400,description='日志筛选参数无效；时间须包含时区，数量为 1–500，任务编号为32位')
    except LookupError:
        abort(404,description='日志页码超出范围')


@pages.get('/api/events')
def events():
    """查询历史事件；参数：任务、级别、带时区时间及游标；返回：JSON。"""
    return jsonify(event_query())


@pages.get('/logs')
def logs():
    """显示分页历史日志；参数：同事件接口；返回：安全转义页面。"""
    filters=log_time_filters()
    data=event_query(filters,page=integer_arg('page',1))
    from ecommerce.web.presentation import present_log_event
    data['events']=[present_log_event(event) for event in data['events']]
    return render_template('logs.html',active='logs',features=FEATURES,
                           shop_name=current_app.config['SETTINGS'].shop_name,time_filters=filters,pagination=data,
                           runtime_alerts=local_task_alerts(current_app.config['SETTINGS'],request.args.get('task_id') or None),**data)


def log_time_filters():
    """解析网页北京时间及快捷范围；参数：当前请求；返回：查询时间和表单回显。"""
    from datetime import datetime, timedelta
    from ecommerce.shared.time import BEIJING
    values={}
    try:
        for name in ('since','until'):
            raw=request.args.get(name,'')
            if not raw:
                values[name]=None
                continue
            if len(raw)>64 or 'T' not in raw:
                raise ValueError('时间格式无效')
            value=datetime.fromisoformat(raw)
            values[name]=value.replace(tzinfo=BEIJING) if value.tzinfo is None else value.astimezone(BEIJING)
        preset=request.args.get('range','')
        if preset:
            end=datetime.now(BEIJING).replace(microsecond=0)
            if preset=='today':
                start=end.replace(hour=0,minute=0,second=0)
            elif preset in {'24h','7d'}:
                start=end-timedelta(hours=24) if preset=='24h' else end-timedelta(days=7)
            else:
                raise ValueError('快捷范围无效')
            values.update(since=start,until=end)
        if values['since'] and values['until'] and values['since']>values['until']:
            raise ValueError('开始时间晚于结束时间')
        return {**{key:value.isoformat() if value else None for key,value in values.items()},
            **{key+'_input':value.replace(tzinfo=None).isoformat(timespec='seconds') if value else '' for key,value in values.items()}}
    except (ValueError,TypeError,OverflowError):
        abort(400,description='时间筛选无效，请选择有效日期时间，并确保开始时间不晚于结束时间')


def collection_query(kind,batch_id=None,*,after_override=None,numbered=False):
    """解析采集筛选并调用统一业务查询；参数：种类及可选内部批次；返回：结果。"""
    from ecommerce.modules.collection.queries import query
    settings=current_app.config['SETTINGS']
    if settings.collection_backend!='mysql':
        abort(409,description='采集数据库尚未启用')
    try:
        parameters={'after':int(request.args.get('after','0')),'limit':int(request.args.get('limit','50'))}
        if numbered:
            parameters={'after':0,'limit':20,'page':integer_arg('page',1),'media_only':True,
                'sort_by':request.args.get('sort'),'order':request.args.get('order'),'with_context':True}
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
@pages.get('/collections/details',endpoint='collection_details')
@pages.get('/collections/searches',endpoint='collection_searches')
def collections():
    """显示商品来源与实际批次；参数：当前筛选；返回：页面。"""
    settings=current_app.config['SETTINGS']
    view='details' if request.path=='/collections/details' else 'searches' if request.path=='/collections/searches' else request.args.get('view','details')
    if view=='products': view='details'
    if view not in {'products','details','searches'}:
        abort(400,description='资料视图无效')
    grouped_searches=view=='searches' and 'category' not in request.args and request.args.get('category_missing')!='1'
    from ecommerce.shared.pagination import pagination
    data={'products':[],'searches':[],**pagination(0,1)}
    if settings.collection_backend=='mysql':
        # 旧商品筛选链接转到独立详情；列表历史在采集历史中查看。
        if request.args.get('product_id') or request.args.get('module'):
            from flask import redirect, url_for
            if request.args.get('product_id'):
                module=request.args.get('module','basic')
                return redirect(url_for('pages.collection_product',product_id=request.args['product_id'],module='history' if module=='list' else module))
        data=browse_query('keyword_groups' if grouped_searches else 'catalog' if view=='details' else view,page=integer_arg('page',1),keyword=request.args.get('keyword',''),source=request.args.get('source',''),
            since=request.args.get('since',''),until=request.args.get('until',''),state=request.args.get('state',''),
            shop=request.args.get('shop',''),origin=request.args.get('origin',''),
            sales_min=request.args.get('sales_min',''),sales_max=request.args.get('sales_max',''),
            sort_by=request.args.get('sort'),order=request.args.get('order'),
            category=request.args.get('category'),category_missing=request.args.get('category_missing')=='1')
    return render_template('product_catalog.html' if view=='details' else 'collections.html',active='collections',features=FEATURES,shop_name=settings.shop_name,
                           enabled=settings.collection_backend=='mysql',view=view,pagination=data,grouped_searches=grouped_searches,**data)


def integer_arg(name, default=0):
    """解析整数参数；参数：名称和默认值；返回：整数或400。"""
    try:
        return int(request.args.get(name,default))
    except (TypeError,ValueError):
        abort(400,description='分页或批次编号无效')


def browse_query(view, **parameters):
    """调用资料浏览业务入口；参数：视图和筛选；返回：结果或明确错误。"""
    from ecommerce.modules.collection.queries import query
    settings=current_app.config['SETTINGS']
    if settings.collection_backend!='mysql':
        abort(409,description='采集数据库尚未启用')
    try:
        return query(settings,'browse_'+view,**parameters)
    except ValueError as error:
        abort(400,description=str(error))
    except LookupError:
        abort(404,description='资料不存在或尚未发布')


@pages.get('/collections/searches/products')
def collection_keyword_products():
    """直接展示搜索词商品及全部观察；参数：搜索词、日期、视图和筛选；返回：只读商品列表。"""
    # 旧链接不再携带已移除的下拉筛选，避免出现用户无法看到的限制。
    deprecated={'sales_metric','sales_period','sales_precision'}
    if deprecated.intersection(request.args):
        from flask import redirect
        from urllib.parse import urlencode
        parameters=[(key,value) for key,values in request.args.lists() if key not in deprecated for value in values]
        return redirect(request.path+'?'+urlencode(parameters))
    data=browse_query('keyword_products',category=request.args.get('category'),category_missing=request.args.get('category_missing')=='1',
        mode=request.args.get('mode','summary'),page=integer_arg('page',1),keyword=request.args.get('keyword',''),
        shop=request.args.get('shop',''),product=request.args.get('product',''),state=request.args.get('state',''),
        ad_status=request.args.get('ad_status',''),
        sales_min=request.args.get('sales_min',''),sales_max=request.args.get('sales_max',''),
        since=request.args.get('since',''),until=request.args.get('until',''),sort_by=request.args.get('sort'),order=request.args.get('order'))
    from ecommerce.web.navigation import safe_return
    settings=current_app.config['SETTINGS']
    return render_template('keyword_products.html',active='collections',features=FEATURES,shop_name=settings.shop_name,
        pagination=data,back_url=safe_return(request.args.get('return_to'),'/collections/searches'),**data)


@pages.get('/collections/products/<product_id>')
def collection_product(product_id):
    """展示商品分区和历史；参数：平台商品编号及模块；返回：详情页面。"""
    data=browse_query('product',product_id=product_id,observation_id=integer_arg('observation'))
    module=request.args.get('module','basic')
    allowed={'basic','history','sources','basic_info','main_images','video','skus','parameters','detail_images','review_summary','reviews','questions'}
    if module not in allowed:
        abort(400,description='商品模块无效')
    selected=None
    batch_id=integer_arg('batch')
    if batch_id:
        selected=next((row for row in data['history'] if row['id']==batch_id and row['product_id']==data['product']['id']),None)
        if selected is None:
            abort(404,description='该批次不属于此商品')
        module=selected['module']
    elif module not in {'basic','history'}:
        context_run=(data['source_context'] or {}).get('context_run',{}).get('id')
        candidates=[row for row in data['history'] if row['module']==module and (
            row['run_id']==context_run if context_run else row['id'] in data['current_ids'])]
        selected=next(iter(candidates),None)
    reviews_view=None
    review_parameters={}
    if module in {'review_summary','reviews'}:
        from ecommerce.modules.collection.review_view import review_page
        summary_batch=integer_arg('summary_batch')
        reviews_batch=integer_arg('reviews_batch')
        if batch_id:
            if selected['module']=='review_summary': summary_batch=batch_id
            else: reviews_batch=batch_id
        try:
            reviews_view=review_page(current_app.config['SETTINGS'],data,summary_batch=summary_batch,reviews_batch=reviews_batch,
                page=integer_arg('page',1),sort_by=request.args.get('sort'),order=request.args.get('order'))
        except ValueError as error:
            abort(400,description=str(error))
        except LookupError as error:
            abort(404,description=str(error))
        overview,comments=reviews_view['overview'],reviews_view['comments']
        review_parameters={'module':'reviews','batch':None,'kind':None,'after':None,'limit':None,
            'summary_batch':overview['selected']['id'] if overview['selected'] else 0,
            'reviews_batch':comments['selected']['id'] if comments['selected'] else 0,
            'tag_page':None,'tag_sort':None,'tag_order':None,
            'page':comments['detail']['page'],'sort':comments['detail']['sort_spec']['sort'],'order':comments['detail']['sort_spec']['order']}
        module='reviews'
        selected=None
    detail=collection_query('records',selected['id'],numbered=True) if selected else None
    media=detail if selected and module in {'video','main_images','detail_images'} else None
    from ecommerce.modules.collection.queries import history_page
    try:
        history_paging=history_page(data['history'],integer_arg('page',1),sort_by=request.args.get('sort'),order=request.args.get('order')) if module=='history' else None
    except ValueError:
        abort(400,description='页码无效')
    except LookupError:
        abort(404,description='历史页码超出范围')
    history_rows=history_paging['history_rows'] if history_paging else []
    sources=browse_query('sources',product_id=product_id,page=integer_arg('page',1),sort_by=request.args.get('sort'),order=request.args.get('order')) if module=='sources' else None
    context_run=(data['source_context'] or {}).get('context_run',{}).get('id')
    display_history=[row for row in data['history'] if row['run_id']==context_run] if context_run else data['history']
    from ecommerce.web.navigation import safe_return
    settings=current_app.config['SETTINGS']
    from ecommerce.modules.collection.web_collection import remaining_plan
    return render_template('collection_product.html',active='collections',features=FEATURES,shop_name=settings.shop_name,
        module=module,selected=selected,detail=detail,media=media,history_paging=history_paging,history_rows=history_rows,
        sources=sources,display_history=display_history,context_run=context_run,reviews_view=reviews_view,review_parameters=review_parameters,
        back_url=safe_return(request.args.get('return_to'),'/collections/details'),collection_plan=remaining_plan(data),**data)


@pages.get('/collections/searches/<task_id>')
def collection_search(task_id):
    """融合本次任务的列表批次；参数：任务、批次、时间、排序和页码；返回：来源列表。"""
    data=browse_query('search_records',task_id=task_id,category=request.args.get('category'),category_missing=request.args.get('category_missing')=='1',
        since=request.args.get('since',''),until=request.args.get('until',''),batch_id=integer_arg('batch'),
        page=integer_arg('page',1),sort_by=request.args.get('sort'),order=request.args.get('order'))
    settings=current_app.config['SETTINGS']
    return render_template('collection_search.html',active='collections',features=FEATURES,shop_name=settings.shop_name,pagination=data,**data)


@pages.get('/tasks/<task_id>')
def task_detail(task_id):
    """展示资料关联任务；参数：任务编号；返回：只读任务详情。"""
    data=browse_query('task',task_id=task_id)
    settings=current_app.config['SETTINGS']
    return render_template('task_detail.html',active='tasks',features=FEATURES,shop_name=settings.shop_name,**data)


@pages.get('/collections/files/<int:file_id>')
def collection_file(file_id):
    """提供已发布素材预览；参数：文件索引编号；返回：受限图片或视频。"""
    from pathlib import Path
    asset=browse_query('file',file_id=file_id)
    root=current_app.config['SETTINGS'].workspace.resolve()
    relative=Path(asset['relative_path'])
    target=(root/relative).resolve()
    allowed={'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.webp':'image/webp',
        '.gif':'image/gif','.avif':'image/avif','.bmp':'image/bmp','.mp4':'video/mp4','.webm':'video/webm'}
    if asset['root_key']!='workspace' or relative.is_absolute() or not target.is_relative_to(root) or target.suffix.lower() not in allowed:
        abort(404)
    # 登录资料、日志等目录即使被错误索引也不对外提供。
    if not target.is_relative_to((root/'output').resolve()) or not target.is_file():
        abort(404,description='本地素材不存在')
    from ecommerce.infrastructure.storage.collection_snapshot import digest_file
    if target.stat().st_size!=asset['size_bytes'] or digest_file(target)!=asset['sha256']:
        abort(409,description='本地素材已改变，需重新核对')
    return send_file(target,mimetype=allowed[target.suffix.lower()],conditional=True)


@pages.get('/collections/batches/<int:batch_id>')
def collection_batch(batch_id):
    """显示批次范围和已采明细；参数：内部批次与分页；返回：页面。"""
    settings=current_app.config['SETTINGS']
    data=collection_query('records',batch_id,numbered=True)
    if data['kind']=='media':
        data['records']=[row for row in data['records'] if row['role'] in {'main_image','detail_image','main_video','sku_image'}]
    return render_template('collection_batch.html',active='collections',features=FEATURES,shop_name=settings.shop_name,pagination=data,**data)
