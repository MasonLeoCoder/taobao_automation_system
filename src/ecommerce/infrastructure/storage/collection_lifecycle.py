"""详情运行的事务收尾与只读状态核对，原快照和已完成模块保持不变。"""
import sqlalchemy as sa
from ecommerce.infrastructure.storage.time_values import database_time,iso_value
from ecommerce.infrastructure.tasks.ownership import owner_alive

TASK_LABELS={'running':'执行中','succeeded':'成功','failed':'失败','cancelled':'已停止',
             'needs_attention':'待人工处理','interrupted':'已中断','stopping':'正在停止','unverified':'状态待核实'}


def execution_state(task, *, stop_requested=False, alive=None):
    """核对执行状态；参数：任务、停止申请、进程身份核对值；返回：显示状态和说明。"""
    if task.get('finished_at'):
        return task['status'],''
    if alive is False:
        return 'interrupted','执行进程已退出，数据库尚未记录结束结论；请核对日志。'
    if alive is None:
        return 'unverified',('停止请求已保存，但' if stop_requested else '')+'无法核实原执行进程，未根据等待时间推断任务已完成。'
    if stop_requested:
        return 'stopping','停止请求已保存，等待执行器确认；尚未宣称已经停止。'
    if task.get('status') not in {'running','needs_attention'}:
        return 'unverified','任务状态缺少对应结束时间，执行结论待核实。'
    return task.get('status') or 'running',''


def task_view(task, *, stop_requested=False):
    """派生任务显示，不修改原状态；参数：任务、停止标记；返回：显示元信息。"""
    task=dict(task or {})
    started=task.get('owner_process_started_at')
    started=started if isinstance(started,str) else iso_value(started)
    try:alive=None if task.get('finished_at') else owner_alive(task.get('owner_pid'),started)
    except (ValueError,TypeError,OSError):alive=None
    state,note=execution_state(task,stop_requested=stop_requested,alive=alive)
    return {'state':state,'label':TASK_LABELS.get(state,state),'note':note}


def finish_runs(connection,tables,operation):
    """与总任务终态同事务收尾未结束详情；参数：事务、表、结束事件；返回：审计摘要。"""
    if not operation.get('close_collection_runs'):
        return None
    runs,steps=tables['collection_runs'],tables['collection_steps']
    pending=list(connection.execute(sa.select(runs).where(runs.c.task_id==operation['task_id'],
        runs.c.status=='running',runs.c.finished_at.is_(None)).order_by(runs.c.id).with_for_update()).mappings())
    state='interrupted' if operation['state'] in {'cancelled','succeeded'} or operation['event_type']=='task_recovered' else operation['state']
    ended=database_time(operation['time'])
    summary=[]
    for run in pending:
        connection.execute(steps.update().where(steps.c.run_id==run['id'],steps.c.requested.is_(True),
            steps.c.finished_at.is_(None),steps.c.status=='running').values(status=state,finished_at=ended,continuation_allowed=False))
        connection.execute(steps.update().where(steps.c.run_id==run['id'],steps.c.requested.is_(True),
            steps.c.finished_at.is_(None),steps.c.status.in_(['pending','not_run'])).values(status='not_run',finished_at=ended,continuation_allowed=False))
        connection.execute(runs.update().where(runs.c.id==run['id']).values(status=state,finished_at=ended,
            error=({'type':'TaskFinalized','message':operation.get('message') or '总任务已结束，未完成详情同步收尾'} if run['error'] is None else run['error'])))
        summary.append(run['id'])
    return {'version':1,'run_ids':summary}


def run_views(connection,tables,records):
    """批量核对运行与任务/进程/停止申请；参数：只读连接、表、运行；返回：保留原列的显示副本。"""
    records=[dict(row) for row in records]
    identifiers={row['task_id'] for row in records if row.get('task_id')}
    tasks=tables['tasks'];jobs=tables.get('collection_jobs');steps=tables['collection_steps']
    owners={row['id']:dict(row) for row in connection.execute(sa.select(tasks).where(tasks.c.id.in_(identifiers))).mappings()} if identifiers else {}
    stopping=set(connection.scalars(sa.select(jobs.c.task_id).where(jobs.c.task_id.in_(identifiers),jobs.c.stop_requested_at.is_not(None)))) if identifiers and jobs is not None else set()
    by_run={row['id']:[] for row in records}
    if by_run:
        for step in connection.execute(sa.select(steps.c.run_id,steps.c.requested,steps.c.status).where(steps.c.run_id.in_(by_run))).mappings():
            if step['requested']:by_run[step['run_id']].append(step['status'])
    checked={}
    for row in records:
        task=owners.get(row.get('task_id'))
        if task and task['id'] not in checked:checked[task['id']]=task_view(task,stop_requested=task['id'] in stopping)
        status=row['status'];note='';ended=row.get('finished_at')
        if status=='interrupted' and task and task.get('finished_at') and task['status']=='cancelled':status='cancelled'
        if status=='running':
            view=checked.get(row.get('task_id'))
            if not view:
                status,note='unverified','缺少关联任务，详情执行状态待核实。'
            elif task.get('finished_at'):
                status='unverified' if view['state']=='succeeded' else view['state']
                note='关联任务已结束，详情运行记录未同步；仅校正显示，未改写历史记录。'
                ended=task['finished_at']
            else:status,note=view['state'],view['note']
        values=by_run[row['id']]
        row['display']={'state':status,'note':note,'finished_at':ended,
            'counts':{'requested':len(values),'completed':sum(v in {'complete','downloaded','no_video','no_images','no_parameters','no_reviews','no_questions','skipped','resolved'} for v in values),
                      'unfinished':sum(v not in {'complete','downloaded','no_video','no_images','no_parameters','no_reviews','no_questions','skipped','resolved'} for v in values)}}
    return records


def task_views(connection,tables,records):
    """批量附加任务状态及本次详情数量；参数：只读连接、表、任务；返回：副本列表。"""
    records=[dict(row) for row in records];identifiers=[row['id'] for row in records]
    jobs=tables.get('collection_jobs');runs,steps=tables.get('collection_runs'),tables.get('collection_steps')
    stopping=set(connection.scalars(sa.select(jobs.c.task_id).where(jobs.c.task_id.in_(identifiers),jobs.c.stop_requested_at.is_not(None)))) if identifiers and jobs is not None else set()
    counts={key:[] for key in identifiers}
    if identifiers and runs is not None and steps is not None:
        for task_id,status in connection.execute(sa.select(runs.c.task_id,steps.c.status).join(steps,steps.c.run_id==runs.c.id).where(
                runs.c.task_id.in_(identifiers),steps.c.requested.is_(True))):counts[task_id].append(status)
    for row in records:
        row['display']=task_view(row,stop_requested=row['id'] in stopping)
        values=counts[row['id']]
        completed=sum(v in {'complete','downloaded','no_video','no_images','no_parameters','no_reviews','no_questions','skipped','resolved'} for v in values)
        row['display']['counts']={'requested':len(values),'completed':completed,'unfinished':len(values)-completed}
        if row.get('finished_at') and row['status']=='succeeded' and len(values)>completed:
            row['display'].update(state='needs_attention',label='执行结束，详情待核对',note='总任务记录成功，但仍有未完成详情步骤；未把任务成功当作详情完整。')
    return records
