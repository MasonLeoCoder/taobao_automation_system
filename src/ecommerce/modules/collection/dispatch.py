"""恢复预览与持久补采队列；短请求只派发，浏览器仍由统一执行器串行运行。"""
from uuid import uuid4
from ecommerce.infrastructure.storage.collection_recovery import RecoveryStore
from ecommerce.infrastructure.tasks import web_jobs as jobs
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.infrastructure.tasks.ownership import execution_key, active_local_tasks, owner_alive
from ecommerce.shared.exceptions import TaskBusy
from ecommerce.modules.collection import recovery
from ecommerce.infrastructure.tasks.ownership import atomic_json
import logging


def store(settings):
    """取得队列存储；参数：配置；返回：存储。"""
    return RecoveryStore(settings)


def pause_marker(settings):
    """本机停止凭据不依赖数据库；参数：配置；返回：固定资源路径。"""
    return settings.workspace/'storage/collection_queue_paused'/f'{execution_key(settings.for_platform("taobao"))}.json'


def pause_queue(settings,reason='用户停止或采集执行中断'):
    """正常暂停写数据库，断库才保留临时凭据；参数：配置、原因；返回：无。"""
    from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
    from ecommerce.infrastructure.storage.mysql import MySQLStorageError
    try:
        CollectionJobStore(settings).pause(execution_key(settings.for_platform('taobao')),True,reason)
        marker=pause_marker(settings)
        if marker.exists():
            import json
            if json.loads(marker.read_text(encoding='utf-8')).get('database',settings.mysql.database)==settings.mysql.database:
                marker.unlink()
    except MySQLStorageError:
        atomic_json(pause_marker(settings),{'reason':reason,'database':settings.mysql.database})
        logging.getLogger(__name__).warning('队列暂停临时保留本机，数据库恢复后优先补写')


def preview(settings, identifier):
    """核验本配置归属再生成恢复预览；参数：配置、原任务；返回：固定范围和动作。"""
    from ecommerce.modules.collection.web_collection import get_job, public_status
    reconcile(settings)
    row = get_job(settings, identifier)
    if not public_status(settings, row)['done']:
        raise TaskBusy('原采集仍在运行，请先停止或等待结束')
    return recovery.preview(settings, identifier)


def resume(settings, identifier, revision, *, submission_key=None):
    """按预览版本重新核对后恢复；参数：原执行及预览版本；返回：新派发状态。"""
    from ecommerce.modules.collection.web_collection import start_configured, _start
    from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
    from ecommerce.modules.collection.web_collection import public_status,get_job
    if submission_key:
        previous=CollectionJobStore(settings).submitted(execution_key(settings.for_platform('taobao')),submission_key)
        if previous:
            if previous.get('resume_task_id')!=identifier or previous.get('resume_revision')!=revision:
                raise ValueError('提交编号与原恢复范围不一致')
            return public_status(settings,previous)
    view = preview(settings, identifier)
    if view['revision'] != revision:
        raise ValueError('恢复计划已经变化，请重新查看预览')
    if view['finished']:
        raise ValueError('原计划已处理结束，请对失败项单独补采')
    options = view['options']
    mode = options['mode']
    from ecommerce.platforms.taobao.detail import resolve_detail_input
    product_id = resolve_detail_input(options['input']).item_id if mode == 'detail' else None
    return _start(settings, product_id, 'selected' if product_id else 'list', mode, options, resume_task_id=identifier,
        resume_revision=revision,submission_key=submission_key,frozen_modules=get_job(settings,identifier).get('modules'))


def retry(settings, identifier, product_id, module):
    """申请单模块重采并沿用原数量范围；参数：来源任务、商品、模块；返回：队列编号。"""
    from ecommerce.modules.collection.web_collection import get_job, normalize_request
    from ecommerce.shared.collection_contract import MODULES
    import re
    if not isinstance(product_id,str) or not re.fullmatch('[1-9][0-9]{0,24}',product_id) or not isinstance(module,str):
        raise ValueError('补采需要有效的商品编号和模块')
    row = get_job(settings, identifier)
    if module not in MODULES:
        raise ValueError('补采模块无效')
    persistence = store(settings)
    original = persistence.read(identifier)
    options = (original or {}).get('options') or row.get('options') or {}
    if original:
        value = original['checkpoint']['products'].get(product_id, {}).get(module)
        if not value:
            raise ValueError('本任务没有该商品模块的采集记录')
        cleanup_tasks = persistence.lineage(original['id'])
    else:
        if row['product_id'] != product_id or module not in row.get('modules', [row['module']]):
            raise ValueError('旧任务不能推断该商品模块范围')
        cleanup_tasks = [identifier]
    request = normalize_request({'mode':'detail','input':product_id,'modules':[module],
        'all_reviews':bool(options.get('all_reviews')) if module == 'reviews' else False,
        'all_questions':bool(options.get('all_questions')) if module == 'questions' else False})
    from ecommerce.modules.collection.web_collection import _start
    result=_start(settings,product_id,'selected','detail',request,cleanup_tasks=cleanup_tasks)
    return {'id':result['id'],'message':'补采申请已入库；等待当前采集结束，暂停队列须手动继续'}


def queue(settings):
    """查询所有待执行申请；参数：配置；返回：公开队列。"""
    from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
    persistence=CollectionJobStore(settings)
    key=execution_key(settings.for_platform('taobao'))
    rows=persistence.rows(key,pending=True)
    rows=[row for row in reversed(rows) if row['dispatch_state'] in {'waiting','paused','claimed'}]
    return [{'id':r['id'],'state':r['dispatch_state'],'product_id':r['product_id'],'module':r['module'],
             'mode':r['mode'],'options':r.get('options'),'created_at':r['created_at']} for r in rows]


def reconcile(settings):
    """空闲资源下补写停止意图和流水，识别失效执行；参数：配置；返回：无。"""
    from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
    from ecommerce.infrastructure.tasks.events import replay_events
    from ecommerce.infrastructure.tasks.collection_control import replay_emergency
    from datetime import datetime
    from ecommerce.infrastructure.storage.database import now
    import json
    persistence=CollectionJobStore(settings)
    platform=settings.for_platform('taobao')
    key=execution_key(platform)
    replay_emergency(settings)
    marker=pause_marker(settings)
    if marker.exists():
        value=json.loads(marker.read_text(encoding='utf-8'))
        if value.get('database',settings.mysql.database)==settings.mysql.database:
            persistence.pause(key,True,value['reason'])
            marker.unlink()
    pending=persistence.rows(key,pending=True)
    dead=[]
    for row in pending:
        if row['dispatch_state'] not in {'claimed','started'}:
            continue
        live=jobs.alive(row)
        age=(datetime.fromisoformat(now())-datetime.fromisoformat(row['updated_at'])).total_seconds()
        if live is False or (not row.get('owner_pid') and age>30):
            dead.append(row)
    if not dead:
        return
    try:
        with shop_lock(platform.profile_path):
            # 先补写实际业务结论，再判断进程中断；恢复只写数据，不打开浏览器。
            replay_events(settings,persistence.tasks)
            persistence.tasks.recover_interrupted(key,owner_alive)
            for row in dead:
                if row['dispatch_state']=='claimed':
                    persistence.fail_start(row['id'],row['claim_token'],'领取后进程已退出，等待重新提交或续采',interrupted=True)
            persistence.pause(key,True,'原采集进程已退出，等待手动继续队列')
    except TaskBusy:
        return


def dispatch_next(settings, *, identifier=None, manual=False):
    """短锁加数据库领取令牌派发，浏览器仍由执行器加锁；参数：指定申请、单次启动；返回：描述或空。"""
    from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
    from ecommerce.infrastructure.tasks.ownership import process_started_at
    persistence=CollectionJobStore(settings)
    reconcile(settings)
    platform=settings.for_platform('taobao')
    with shop_lock(jobs.directory(settings)):
        if active_local_tasks(platform):
            return None
        selected=persistence.claim(execution_key(platform),identifier,manual=manual)
        if selected is None:
            return None
        try:
            process=jobs.launch(settings,selected['id'],selected['claim_token'])
            persistence.bind_process(selected['id'],selected['claim_token'],process.pid,process_started_at(process.pid))
        except Exception:
            persistence.fail_start(selected['id'],selected['claim_token'],'后台进程未能启动，未执行采集')
            persistence.pause(execution_key(platform),True,'后台启动失败，请核对后继续队列')
            logging.getLogger(__name__).exception('采集申请已入库，但后台启动失败')
        return persistence.read(selected['id'])


def control_queue(settings, action):
    """明确继续或暂停自动派发；参数：动作；返回：队列。"""
    from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
    if action not in {'pause','resume'}:
        raise ValueError('队列动作只能为resume或pause')
    reconcile(settings)
    if action=='pause':
        pause_queue(settings,'用户暂停队列')
    else:
        CollectionJobStore(settings).pause(execution_key(settings.for_platform('taobao')),False,'用户明确继续队列')
        dispatch_next(settings)
    return queue(settings)


def after_execution(settings, identifier, status):
    """结论已落库后处理后续派发；参数：任务、结论；返回：无。"""
    if status!='succeeded':
        pause_queue(settings)
    else:
        dispatch_next(settings)


def on_web_start(settings):
    """重启保留活跃执行，等待申请暂停，不自动采集；参数：配置；返回：无。"""
    from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
    reconcile(settings)
    persistence=CollectionJobStore(settings)
    key=execution_key(settings.for_platform('taobao'))
    if persistence.rows(key,pending=True):
        persistence.pause(key,True,'服务重启，等待手动继续队列；当前活跃采集继续执行')
