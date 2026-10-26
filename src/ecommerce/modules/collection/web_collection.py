"""关键词、任意商品详情及补采的网页派发、状态和定向反馈。"""
from dataclasses import asdict,replace
from datetime import datetime
import json
import re
from uuid import uuid4

from ecommerce.infrastructure.tasks import web_jobs as jobs
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.infrastructure.tasks.ownership import active_local_tasks, execution_key, process_started_at
from ecommerce.modules.collection.queries import query
from ecommerce.shared.exceptions import TaskBusy
from ecommerce.infrastructure.storage.database import now

MODULES=('basic_info','video','main_images','skus','parameters','detail_images','review_summary','reviews','questions')
COMPLETE_STATES={'complete','downloaded','no_video','no_images','no_parameters','no_reviews','no_questions'}


def remaining_plan(data):
    """按商品全局有效分区计算剩余模块；参数：不带来源限制的商品资料；返回：有序计划。"""
    current=set(data['current_ids'])
    completed={item['module'] for item in data['history'] if item['id'] in current and item['status'] in COMPLETE_STATES}
    return {'modules':[module for module in MODULES if module not in completed],
            'skipped_modules':[module for module in MODULES if module in completed]}


def plan(settings,product_id):
    """读取当前商品补采计划；参数：配置、商品；返回：计划，不受旧搜索/运行上下文限制。"""
    return remaining_plan(validate(settings,product_id))


def validate(settings, product_id, module=None):
    """校验当前商品和模块；参数：配置、平台商品编号、模块；返回：商品资料。"""
    if settings.task_backend!='mysql' or settings.collection_backend!='mysql':
        raise ValueError('网页采集需要同时启用 MySQL 任务和采集存储')
    if not isinstance(product_id,str) or not re.fullmatch('[1-9][0-9]{0,24}',product_id):
        raise ValueError('商品编号无效')
    if module is not None and (not isinstance(module,str) or module not in MODULES):
        raise ValueError('不支持的单模块采集')
    return query(settings,'browse_product',product_id=product_id)


def normalize_request(body):
    """严格解析新建采集参数；参数：网页 JSON；返回：脱敏后的固定配置。"""
    from ecommerce.platforms.taobao.search import validate_keyword
    from ecommerce.platforms.taobao.cards import validate_limit
    from ecommerce.platforms.taobao.pagination import validate_pages
    from ecommerce.platforms.taobao.detail import resolve_detail_input
    if not isinstance(body,dict):
        raise ValueError('采集参数必须为对象')
    mode=body.get('mode')
    if mode=='keyword':
        if set(body)-{'mode','keyword','pages','limit','with_details'}:
            raise ValueError('关键词采集包含不支持的参数')
        details=body.get('with_details',False)
        if type(details) is not bool:
            raise ValueError('连带详情选项必须为布尔值')
        return {'mode':mode,'keyword':validate_keyword(body.get('keyword')),
                'pages':validate_pages(body.get('pages',1)),'limit':validate_limit(body.get('limit')),'with_details':details}
    if mode=='detail':
        if set(body)-{'mode','input','modules','all_reviews','all_questions'}:
            raise ValueError('详情采集包含不支持的参数')
        target=resolve_detail_input(body.get('input'))
        if not re.fullmatch('[1-9][0-9]{0,24}',target.item_id):
            raise ValueError('商品编号须为不含前导零的正整数')
        modules=body.get('modules')
        if (not isinstance(modules,list) or not modules or
                any(not isinstance(module,str) or module not in MODULES for module in modules)
                or len(set(modules))!=len(modules)):
            raise ValueError('请选择至少一个有效模块，且不能重复')
        options={key:body.get(key,False) for key in ('all_reviews','all_questions')}
        for key,module in (('all_reviews','reviews'),('all_questions','questions')):
            if type(options[key]) is not bool or options[key] and module not in modules:
                raise ValueError('不限数量仅适用于已选中的评价或问大家模块')
        # 去除链接跟踪、授权参数，同时保留淘宝/天猫入口区别。
        return {'mode':mode,'input':target.item_id if target.input_type=='id' else target.canonical_url,
                'modules':[module for module in MODULES if module in modules],**options}
    raise ValueError('请选择关键词采集或商品详情采集')


def validate_job(settings,row):
    """按入口验证执行配置；参数：配置、派发；返回：无，新商品无需预先存在。"""
    if settings.task_backend!='mysql' or settings.collection_backend!='mysql':
        raise ValueError('网页采集需要同时启用 MySQL 任务和采集存储')
    if row.get('mode') in {'keyword','detail'}:
        normalized=normalize_request(row['options'])
        if normalized!=row['options'] or normalized['mode']!=row['mode']:
            raise ValueError('采集参数与已确认配置不一致')
        if row['mode']=='detail':
            from ecommerce.platforms.taobao.detail import resolve_detail_input
            if resolve_detail_input(normalized['input']).item_id!=row['product_id'] or normalized['modules']!=row['modules']:
                raise ValueError('商品或模块与派发配置不一致')
    else:
        validate(settings,row['product_id'],None if row.get('mode')=='remaining' else row['module'])


def start_configured(settings,body):
    """从配置页新建任务；参数：配置、请求；返回：可恢复的后台任务状态。"""
    options=normalize_request(body)
    product_id=None
    if options['mode']=='detail':
        from ecommerce.platforms.taobao.detail import resolve_detail_input
        product_id=resolve_detail_input(options['input']).item_id
    selected=options.get('modules',[])
    validate_job(settings,{'mode':options['mode'],'options':options,'product_id':product_id,'modules':selected})
    return _start(settings,product_id,'list' if options['mode']=='keyword' else 'selected',options['mode'],options)


def start(settings, product_id, module):
    """互斥派发单模块；参数：配置、商品、模块；返回：派发描述，相同在途请求复用任务。"""
    validate(settings,product_id,module)
    return _start(settings,product_id,module,'single')


def start_remaining(settings,product_id):
    """互斥派发当前商品所有未完成模块；参数：配置、商品；返回：派发，不开启评价问答全量。"""
    validate(settings,product_id)
    return _start(settings,product_id,'remaining','remaining')


def _start(settings,product_id,module,mode,options=None):
    """持短锁派发并冻结本次计划；参数：配置、商品、入口、模式；返回：公开状态。"""
    platform=settings.for_platform('taobao')
    with shop_lock(jobs.directory(settings)):
        for row in jobs.rows(settings):
            state=public_status(settings,row)
            if not state['done']:
                if (row['product_id']==product_id and row['module']==module
                        and row.get('mode','single')==mode and row.get('options')==options):
                    return state
                raise TaskBusy('已有网页采集任务进行中，请先完成或停止当前任务')
        if active_local_tasks(platform):
            raise TaskBusy('淘宝浏览器已有任务运行，请先完成当前任务')
        selected=plan(settings,product_id) if mode=='remaining' else {
            'modules':options.get('modules',[]) if options else [module],'skipped_modules':[]}
        if mode!='keyword' and not selected['modules']:
            raise ValueError('该商品九个模块均有有效完成结果，无需采集')
        identifier=uuid4().hex
        # 配置对象只有密码环境变量名，没有密码；子进程精确复用本次网页配置。
        config=json.loads(json.dumps(asdict(settings),default=str))
        row={'id':identifier,'product_id':product_id,'module':module,'database':settings.mysql.database,
             'mode':mode,**selected,
             'execution_key':execution_key(platform),'created_at':now(),'state':'queued',
             'owner_pid':None,'owner_process_started_at':None,'config':config,
             'message':'正在启动后台补采任务' if mode=='remaining' else '正在启动后台单模块采集','result':None}
        if options:
            row.update(options=options,message='正在启动关键词采集' if mode=='keyword' else '正在启动所选详情采集')
        jobs.save(settings,row)
        try:
            process=jobs.launch(settings,identifier)
            row.update(owner_pid=process.pid,owner_process_started_at=process_started_at(process.pid))
            jobs.save(settings,row)
        except Exception:
            row.update(state='failed',message='后台进程未能启动，未执行采集')
            jobs.save(settings,row)
            raise
        return public_status(settings,row)


def bound_job(settings, product_id, identifier):
    """绑定当前配置和商品；参数：配置、商品、派发编号；返回：已校验派发。"""
    row=jobs.read(settings,identifier)
    if (row['product_id']!=product_id or row['database']!=settings.mysql.database
            or row['execution_key']!=execution_key(settings.for_platform('taobao'))):
        raise LookupError('任务不属于当前商品或执行配置')
    return row


def get_job(settings,identifier):
    """读取并核验本执行配置内的任务；参数：配置、派发编号；返回：派发记录。"""
    row=jobs.read(settings,identifier)
    return bound_job(settings,row['product_id'],identifier)


def public_status(settings, row):
    """产生可供网页展示的真实状态；参数：配置、派发；返回：不含配置、路径或凭据的状态。"""
    state=row['state']
    done=state not in {'queued','running'}
    message=row['message']
    prompt=None
    storage_pending=False
    halted=False
    if not done:
        live=jobs.alive(row)
        if live is False:
            state,done,message='interrupted',True,'后台进程已退出；已保存资料保留，请查看任务日志后重试'
        elif live is None:
            age=(datetime.fromisoformat(now())-datetime.fromisoformat(row['created_at'])).total_seconds()
            if age>30:
                state,message='unverified','无法核实后台进程，请核对本地进程；不会自动重新采集'
        if not done:
            active=next((task for task in active_local_tasks(settings.for_platform('taobao')) if task['id']==row['id']),{})
            storage_pending=bool(active.get('storage_pending'))
            path=settings.workspace/'storage/task_controls'/f"{row['id']}.state.json"
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                if data.get('task_id')==row['id'] and data.get('prompt_id') and live is True:
                    prompt={key:data.get(key) for key in ('prompt_id','allowed','message','step')}
                    state,message='needs_attention','等待人工处理，请在浏览器处理后选择继续或停止'
            except FileNotFoundError:
                pass
            if storage_pending:
                state,message='storage_pending','正在等待数据库恢复；可以停止，继续不能跳过入库确认'
            if live is True and active:
                from ecommerce.modules.monitoring.service import local_task_alerts
                alerts=local_task_alerts(settings,row['id'],active_tasks=[active])
                if alerts:
                    alert=alerts[0]
                    prompt=None
                    if alert['state']=='halted':
                        halted=True
                        state,message='needs_attention','采集已中断，浏览器仅保留现场；原因：'+alert['message']+'。请停止当前任务后重新发起，已有记录和待入库流水保留。'
                    elif alert['state']=='unverified':
                        state,message='unverified',alert['message']+'；请核对日志，不能继续或确认，停止入口仍可用'
                    else:
                        storage_pending=True
                        state,message='storage_pending',alert['message']+'；可以停止，继续不能跳过入库确认'
    result=row.get('result') or {}
    if state in {'interrupted','verification_pending'}:
        # 进程退出或结论文件未写完时，只恢复数据库结果，不重放采集动作。
        from ecommerce.infrastructure.storage.mysql import MySQLStorageError
        try:
            task=query(settings,'browse_task',task_id=row['id'])['task']
            if task.get('finished_at'):
                state,message=task['status'],task.get('message') or '任务已结束'
                result=saved_result(settings,row)
                if state=='succeeded' and not result.get('satisfied',result['valid']):
                    state,message='needs_attention','任务已结束，尚未确认有效模块资料；请核对日志和采集尝试'
        except LookupError:
            pass
        except MySQLStorageError:
            message+='；数据库暂不可用，结果待核对'
    return {'id':row['id'],'product_id':row['product_id'],'module':row['module'],'state':state,'done':done,
            'mode':row.get('mode','single'),'created_at':row['created_at'],
            'options':row.get('options'), 'progress':row.get('progress',{}),
            'message':message,'prompt':prompt,'storage_pending':storage_pending,'halted':halted,
            'batch_id':result.get('batch_id'),'valid':bool(result.get('valid')),
            'skipped':bool(result.get('skipped')),'skip_reason':result.get('reason') if result.get('skipped') else None,
            'result_label':result.get('label','查看采集结果'),
            **bulk_progress(row,result)}


def bulk_progress(row,result):
    """返回多模块在途或终态进度；参数：派发、已核对结果；返回：公开进度。"""
    progress=row.get('progress') or {}
    results=result.get('modules',progress.get('results',[]))
    selected=row.get('modules',[row['module']])
    return {'modules':selected,'skipped_modules':row.get('skipped_modules',[]),'results':results,
            'current_module':progress.get('current_module'),'started_modules':progress.get('started_modules',[]),
            'completed_count':sum(bool(item['valid']) for item in results),
            'skipped_count':sum(bool(item.get('skipped')) for item in results) or int(bool(result.get('skipped'))),
            'total_count':len(selected)}


def saved_result(settings,row):
    """核对本任务已发布模块，不能用旧结果代替；参数：配置和派发；返回：结果定位。"""
    if row.get('mode')=='keyword':
        try:
            pages=query(settings,'browse_search',task_id=row['id'])['pages']
        except LookupError:
            pages=[]
        count=(row.get('progress') or {}).get('completed_page_count',0)
        return {'batch_id':pages[0]['id'] if pages else None,'valid':bool(pages) and len(pages)>=count,
                'label':'查看采集列表','page_count':len(pages)}
    bulk=row.get('mode') in {'remaining','detail'}
    if row.get('mode')=='detail':
        try:
            data=validate(settings,row['product_id'])
        except LookupError:
            data={'history':[],'current_ids':[]}
    else:
        data=validate(settings,row['product_id'],None if bulk else row['module'])
    if bulk:
        results=[saved_module(settings,row,data,module) for module in row['modules']]
        return {'batch_id':None,'valid':all(item['valid'] for item in results),
                'satisfied':all(item['valid'] or item.get('skipped',False) for item in results),
                'modules':results,'label':'查看商品资料'}
    return saved_module(settings,row,data,row['module'])


def saved_module(settings,row,data,module):
    """核对模块资料或同任务跳过依据；参数：配置、派发、商品资料、模块；返回：独立结论。"""
    result=module_result(data,row['id'],module)
    if module=='questions':
        evidence=query(settings,'browse_module_skip',task_id=row['id'],product_id=row['product_id'])
        if evidence.get('skipped'):
            result.update(batch_id=None,valid=False,skipped=True,satisfied=True,reason='entry_not_found',
                          message=evidence['message'],label='查看任务日志')
    return result


def module_result(data,task_id,module):
    """定位同任务同模块已发布结果；参数：商品资料、任务、模块；返回：批次与独立有效性。"""
    batch=next((item for item in data['history'] if item.get('task_id')==task_id and item['module']==module),None)
    valid=bool(batch and batch['id'] in data['current_ids'] and batch['status'] in COMPLETE_STATES)
    return {'module':module,'batch_id':batch['id'] if batch else None,'valid':valid,
            'label':'查看采集结果' if batch and batch['status'].startswith('no_') else '查看资料'}


def status(settings, product_id, identifier):
    """读取指定商品任务；参数：配置、商品、编号；返回：公开状态。"""
    return public_status(settings,bound_job(settings,product_id,identifier))


def recent(settings, product_id):
    """恢复当前商品每个模块最近任务；参数：配置、商品；返回：最近派发状态。"""
    result={}
    for row in jobs.rows(settings):
        if row['product_id']==product_id and row['module'] not in result:
            bound_job(settings,product_id,row['id'])
            result[row['module']]=public_status(settings,row)
    return list(result.values())


def control(settings, product_id, identifier, action, prompt_id=None):
    """只向当前商品当前任务反馈；参数：配置、商品、任务、指令、提示编号；返回：接收说明。"""
    if not isinstance(action,str) or action not in {'continue','confirm','cancel','stop'}:
        raise ValueError('反馈只能选择继续、确认、取消或停止')
    with shop_lock(jobs.directory(settings)):
        row=bound_job(settings,product_id,identifier)
        state=public_status(settings,row)
        if state['done']:
            raise ValueError('任务已经结束，请刷新资料')
        if action=='stop' and row['state']=='queued':
            row.update(state='cancelled',message='已取消等待启动的采集，未执行浏览器操作')
            jobs.save(settings,row)
            return row['message']
        if action!='stop' and (not prompt_id or prompt_id!=(state['prompt'] or {}).get('prompt_id')):
            raise ValueError('人工提示已更新，请等待当前弹窗后重新选择')
        path=settings.workspace/'storage/task_controls'/f'{identifier}.json'
        if path.exists() and action!='stop':
            raise ValueError('上一条指令尚未接收，请稍候')
        from ecommerce.modules.account.taobao_control import request_control
        return request_control(settings.for_platform('taobao'),action,task_id=identifier,prompt_id=prompt_id)


def active(settings):
    """恢复跨页面在途任务；参数：配置；返回：公开状态列表。"""
    result=[]
    for row in jobs.rows(settings):
        if row['execution_key']==execution_key(settings.for_platform('taobao')):
            state=public_status(settings,row)
            if not state['done']:
                result.append(state)
    return result


def history(settings):
    """恢复配置页最近任务；参数：配置；返回：最近二十个本执行配置的公开状态。"""
    selected=[row for row in jobs.rows(settings)
              if row['execution_key']==execution_key(settings.for_platform('taobao'))][:20]
    return [public_status(settings,row) for row in selected]


def run_collection(settings, row):
    """在同一资源锁和浏览器中执行所选模块；参数：配置、派发；返回：统一任务结果。"""
    from ecommerce.infrastructure.tasks.runner import run_task
    from ecommerce.modules.collection.detail_service import open_detail_input
    browser_settings=replace(settings.for_platform('taobao'),headless=False)
    if row.get('mode')=='keyword':
        from ecommerce.modules.collection.service import collect_keyword
        options=row['options']
        return run_task(browser_settings,'collection.taobao.collect',lambda:collect_keyword(
            browser_settings,options['keyword'],pages=options['pages'],limit=options['limit'],with_details=options['with_details'],
            wait=False,on_attention=web_attention,on_login_required=web_attention,
            on_progress=lambda values:keyword_progress(settings,row,values)),task_id=row['id'])
    def action():
        """持有浏览器资源锁后复查补采范围，只执行剩余项目；参数：无；返回：采集说明。"""
        if row.get('mode')=='remaining':
            selected=plan(settings,row['product_id'])
            row.update(selected)
            row['progress']={'current_module':None,'started_modules':[],'results':[]}
            jobs.save(settings,row)
            from ecommerce.infrastructure.tasks.events import current_events
            session=current_events()
            if session:
                session.emit('task_progress','已核对当前商品补采范围',details=selected)
            if not row['modules']:
                return '该商品九个模块已完成，本次未打开浏览器'
            flags={module:module in row['modules'] for module in MODULES}
            return open_detail_input(browser_settings,row['product_id'],wait=False,on_attention=web_attention,
                                     on_login_required=web_attention,on_module_event=lambda module,stage:module_progress(settings,row,module,stage),**flags)
        if row.get('mode')=='detail':
            row['progress']={'current_module':None,'started_modules':[],'results':[]}
            jobs.save(settings,row)
            flags={module:module in row['modules'] for module in MODULES}
            return open_detail_input(browser_settings,row['options']['input'],wait=False,
                on_attention=web_attention,on_login_required=web_attention,
                all_reviews=row['options']['all_reviews'],all_questions=row['options']['all_questions'],
                on_module_event=lambda module,stage:module_progress(settings,row,module,stage),**flags)
        flags={module:module==row['module'] for module in MODULES}
        return open_detail_input(browser_settings,row['product_id'],wait=False,
                                 on_attention=web_attention,on_login_required=web_attention,**flags)
    return run_task(browser_settings,'collection.taobao.detail.open',
                    action,task_id=row['id'])


def keyword_progress(settings,row,values):
    """保存关键词在途计数；参数：配置、派发、已完成事实；返回：无。"""
    row.setdefault('progress',{}).update(values)
    jobs.save(settings,row)


def module_progress(settings,row,module,stage):
    """发布当前步骤与已入库进度；参数：配置、派发、模块、阶段；返回：无，不重放采集。"""
    progress=row['progress']
    if stage=='running':
        progress['current_module']=module
        if module not in progress['started_modules']:
            progress['started_modules'].append(module)
    else:
        data=validate(settings,row['product_id'])
        value=saved_module(settings,row,data,module)
        progress['results']=[item for item in progress['results'] if item['module']!=module]+[value]
        progress['current_module']=None
    jobs.save(settings,row)


def web_attention(message):
    """等待网页明确反馈，页面复核仍由原守卫完成；参数：原因；返回：继续、确认或取消。"""
    from ecommerce.platforms.taobao.verification import active_guard
    from ecommerce.infrastructure.tasks.events import current_events,storage_checkpoint
    from ecommerce.shared.exceptions import TaskStopped
    guard=active_guard()
    if guard is None:
        raise RuntimeError('人工处理缺少淘宝验证守卫')
    guard.feedback_file_only=True
    native=message.startswith(('【需要人工决定原生弹窗】','【原生弹窗尚未决定】'))
    session=current_events()
    if session:
        session.progress('needs_attention',message)
        session.emit('manual_wait',message,state='needs_attention')
    reply=guard.read_feedback(message,allowed={'确认','取消'} if native else {'继续'})
    if reply=='停止':
        raise TaskStopped('用户已停止网页采集任务')
    storage_checkpoint('网页人工反馈后复核')
    if session:
        session.progress('running','已收到人工反馈，等待平台守卫复核')
        session.emit('manual_feedback','网页人工反馈',state='running',details={'decision':reply})
    return reply
