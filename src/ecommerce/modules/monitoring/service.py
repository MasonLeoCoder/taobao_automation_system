"""命令行与网页共享的只读运行状态查询。"""
import json
from ecommerce.infrastructure.storage.database import list_tasks, task_counts
from ecommerce.shared.config import Settings
from ecommerce.shared.features import FEATURES


def local_task_alerts(settings: Settings, task_id=None, *, active_tasks=None):
    """查询已核实本地任务的异常提示；参数：配置、可选任务及归属；返回：不冒充入库结果的提示。"""
    if settings.task_backend!='mysql':
        return []
    from ecommerce.infrastructure.tasks.events import pending_task_status,sanitize
    from ecommerce.infrastructure.tasks.ownership import active_local_tasks
    active=active_tasks if active_tasks is not None else active_local_tasks(settings.for_platform('taobao'))
    alerts=[]
    for task in active:
        identifier=task['id']
        if task_id and identifier!=task_id:
            continue
        status=pending_task_status(settings,identifier)
        path=settings.workspace/'storage/task_controls'/f'{identifier}.state.json'
        try:
            state=json.loads(path.read_text(encoding='utf-8'))
            if isinstance(state,dict) and state.get('task_id')==identifier and state.get('halted') is True:
                reason=state.get('message')
                status={'state':'halted','message':sanitize(reason if isinstance(reason,str) and reason else '业务已中断，浏览器仅保留现场'),
                        'step':state.get('step'),'source':'local_control','time':None}
        except (OSError,ValueError,TypeError):
            pass
        if status:
            alerts.append({'task_id':identifier,**status})
    if active_tasks is None:
        # 已停止的网页任务也可能有未入库的真实结束事件，不能又回退成最后一条等待日志。
        from ecommerce.infrastructure.tasks import web_jobs
        from ecommerce.infrastructure.tasks.ownership import execution_key,owner_alive
        active_ids={task['id'] for task in active}
        for row in web_jobs.rows(settings):
            identifier=row['id']
            if identifier in active_ids or task_id and identifier!=task_id:
                continue
            if (row.get('database')!=settings.mysql.database
                    or row.get('execution_key')!=execution_key(settings.for_platform('taobao'))
                    or owner_alive(row.get('owner_pid'),row.get('owner_process_started_at')) is not False):
                continue
            status=pending_task_status(settings,identifier)
            if status and status['state']=='finished_pending':
                alerts.append({'task_id':identifier,**status})
    return alerts


def recent_tasks(settings: Settings) -> list[dict]:
    """查询近期任务；参数：配置；返回：任务记录列表。"""
    return list_tasks(settings.task_database)


def task_history(settings: Settings, page: int = 1, *, sort_by=None,order=None) -> dict:
    """分页查询全部任务；参数：配置、从1开始页码；返回：每页20条及分页信息。"""
    if type(page) is not int or page<1:
        raise ValueError('页码须为正整数')
    source=settings.task_database
    total=sum(task_counts(source).values())
    from ecommerce.shared.pagination import PAGE_SIZE,pagination
    info=pagination(total,page)
    from ecommerce.shared.sorting import sorting
    spec=sorting(sort_by,order,{'name','status','started_at','finished_at','message'},'started_at','desc')
    return {'tasks':list_tasks(source,PAGE_SIZE,offset=(page-1)*PAGE_SIZE,sort_by=sort_by,order=order),**info,'sort_spec':spec,
            'features':FEATURES,'shop_name':settings.shop_name}


def overview(settings: Settings) -> dict:
    """查询工作台状态；参数：配置；返回：功能、任务统计和近期记录。"""
    counts = task_counts(settings.task_database)
    return {"features": FEATURES, "tasks": recent_tasks(settings), "counts": counts,
            "total": sum(counts.values()), "shop_name": settings.shop_name,
            "has_session_file": settings.session_path.is_file()}
