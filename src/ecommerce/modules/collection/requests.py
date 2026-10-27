"""命令行采集登记同一数据库申请，空闲时同步执行，繁忙时保留排队。"""
from dataclasses import asdict
import json
import os
from uuid import uuid4
from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
from ecommerce.infrastructure.storage.database import now
from ecommerce.infrastructure.tasks.ownership import execution_key, process_started_at, active_local_tasks
from ecommerce.infrastructure.tasks.runner import TaskResult


def execute(settings, name, action, options, *, runner, resume_task_id=None, resume_revision=None):
    """登记后同步执行或排队；参数：配置、名称、动作、固定参数及执行器；返回：任务结果。"""
    if settings.task_backend!='mysql' or settings.collection_backend!='mysql':
        return runner(settings,name,action)
    from ecommerce.platforms.taobao.detail import resolve_detail_input
    from ecommerce.shared.collection_contract import MODULES
    from ecommerce.modules.collection.dispatch import reconcile
    reconcile(settings)
    product_id=resolve_detail_input(options['input']).item_id if options['mode']=='detail' else None
    modules=options.get('modules',list(MODULES) if options.get('with_details') else [])
    row={'id':uuid4().hex,'product_id':product_id,'module':'selected' if product_id else 'list',
         'mode':options['mode'],'options':options,'modules':modules,'skipped_modules':[],
         'execution_key':execution_key(settings),'created_at':now(),
         'config':json.loads(json.dumps(asdict(settings),default=str))}
    if resume_task_id:
        row.update(resume_task_id=resume_task_id,resume_revision=resume_revision)
    store=CollectionJobStore(settings)
    job_id=store.submit(row,submission_key=uuid4().hex,origin='cli')
    claimed=None if active_local_tasks(settings) else store.claim(execution_key(settings),job_id,manual=bool(resume_task_id))
    if claimed is None:
        return TaskResult(job_id,'queued','采集申请已入库排队；可在网页查看或继续队列',0)
    store.bind_process(job_id,claimed['claim_token'],os.getpid(),process_started_at(os.getpid()))
    result=runner(settings,name,action,task_id=job_id,job_claim_token=claimed['claim_token'])
    if result.task_id is None:
        store.fail_start(job_id,claimed['claim_token'],result.message)
        store.pause(execution_key(settings),True,'命令行采集未能启动，等待核对')
    return result
