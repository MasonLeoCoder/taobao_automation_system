"""数据库采集控制与断库紧急停止；本地停止意图不会自动启动任何采集。"""
import json
from uuid import uuid4
from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore, JobConflict, identifier
from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from ecommerce.infrastructure.tasks.ownership import active_local_tasks, atomic_json, execution_key


def session():
    """取得已绑定数据库申请的执行会话；参数：无；返回：会话或空。"""
    from ecommerce.infrastructure.tasks.events import current_events
    current=current_events()
    return current if current and getattr(current,'job_claim_token',None) else None


def emergency_stop(settings, job_id):
    """核实本地进程后持久保存停止意图；参数：配置、申请；返回：操作说明。"""
    identifier(job_id)
    matches=[row for row in active_local_tasks(settings) if row['id']==job_id]
    if len(matches)!=1:
        raise ValueError('无法核实该任务的活跃进程，未发送停止指令')
    owner=matches[0]
    request={'id':uuid4().hex,'task_id':job_id,'action':'stop','database':settings.mysql.database,
        'execution_key':execution_key(settings),'owner_pid':owner['owner_pid'],
        'owner_process_started_at':owner['owner_process_started_at']}
    atomic_json(settings.workspace/'storage/collection_emergency_stops'/f'{job_id}.json',request)
    atomic_json(settings.workspace/'storage/task_controls'/f'{job_id}.json',request)
    return '数据库暂不可用，紧急停止已暂存并通知已核实进程；恢复连接后优先补写停止及队列暂停'


def replay_emergency(settings):
    """恢复数据库中的停止与暂停，不重放浏览器；参数：配置；返回：无。"""
    store=CollectionJobStore(settings)
    from ecommerce.infrastructure.storage.time_values import database_time
    for path in (settings.workspace/'storage/collection_emergency_stops').glob('*.json'):
        request=json.loads(path.read_text(encoding='utf-8'))
        if request.get('database')!=settings.mysql.database:
            continue
        row=store.read(request['task_id'])
        if (row['execution_key']!=request['execution_key'] or row['owner_pid']!=request['owner_pid']
                or database_time(row['owner_process_started_at'])!=database_time(request['owner_process_started_at'])):
            raise JobConflict('紧急停止记录进程归属不匹配，保留记录待核对')
        store.pause(row['execution_key'],True,'断库期间用户停止，恢复后保持队列暂停')
        try:
            store.control(row['id'],'stop',command_id=request['id'])
        except JobConflict:
            if not store.read(row['id']).get('actual_finished'):
                raise
        path.unlink()


def local_stop(current):
    """只接收绑定本次进程的紧急停止；参数：执行会话；返回：指令或空。"""
    path=current.settings.workspace/'storage/task_controls'/f'{current.task_id}.json'
    if not path.exists():
        return None
    request=json.loads(path.read_text(encoding='utf-8'))
    if request.get('action')!='stop':
        return None
    if request.get('owner_pid') is not None and (request['owner_pid']!=current.owner['owner_pid']
            or request.get('owner_process_started_at')!=current.owner['owner_process_started_at']
            or request.get('database')!=current.store.settings.database):
        raise JobConflict('紧急停止指令不属于当前进程')
    path.unlink()
    return request


def receive(*, stop_only=False):
    """读取当前数据库指令，断库只接受紧急停止；参数：只取停止；返回：是否托管、指令。"""
    current=session()
    if current is None:
        return False,None
    request=local_stop(current)
    if request:
        return True,request
    try:
        return True,CollectionJobStore(current.settings).receive_control(current.task_id,current.job_claim_token,stop_only=stop_only)
    except MySQLStorageError as error:
        current.error=error
        if not current.waiting:
            current.collection_barrier=current.append(current.operation('event','control_storage_wait','控制数据库暂不可用，暂停后续动作',level='WARNING'))
            current.checkpoint('等待控制数据库恢复')
        return True,None


def publish(prompt):
    """将当前人工提示排入持久事件流水；参数：提示；返回：是否由数据库管理。"""
    current=session()
    if current is None:
        return False
    current.current_prompt=prompt
    if prompt and prompt.get('halted'):
        # 原流水被不可重试错误阻断时，诊断仍需能从数据库看到；业务结果不在此补写。
        try:
            CollectionJobStore(current.settings).prompt(current.task_id,current.job_claim_token,prompt)
        except MySQLStorageError:
            pass
    operation=current.operation('job_prompt','collection_prompt','采集人工提示已更新')
    operation.update(job_claim_token=current.job_claim_token,prompt=prompt)
    current.collection_barrier=current.append(operation)
    return True
