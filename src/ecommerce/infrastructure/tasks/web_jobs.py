"""数据库采集申请入口；本地目录仅保留进程日志和跨进程锁。"""
from dataclasses import asdict
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ecommerce.infrastructure.tasks.ownership import owner_alive
from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
from ecommerce.infrastructure.storage.database import now


def directory(settings):
    """返回派发目录；参数：配置；返回：工作空间内的目录。"""
    return settings.workspace / 'storage/web_collection_jobs'


def read(settings, identifier):
    """读取数据库申请；参数：配置、编号；返回：已关联执行的记录。"""
    return CollectionJobStore(settings).read(identifier)


def save(settings, row):
    """保存展示进度，执行内先持久缓冲再入库；参数：配置、申请；返回：无。"""
    from ecommerce.infrastructure.tasks.events import current_events
    session=current_events()
    if session and session.task_id==row['id']:
        operation=session.operation('job_runtime','collection_progress','采集展示进度已更新')
        operation.update(job_claim_token=row.get('claim_token'),runtime={k:row[k] for k in ('progress','recovery_ready') if k in row})
        session.collection_barrier=session.append(operation)
        session.checkpoint('确认采集展示进度')
    else:
        CollectionJobStore(settings).runtime(row)


def rows(settings, *, pending=False, limit=200):
    """读取当前资源数据库申请；参数：配置、在途开关、上限；返回：最近记录。"""
    from ecommerce.infrastructure.tasks.ownership import execution_key
    return CollectionJobStore(settings).rows(execution_key(settings.for_platform('taobao')),pending=pending,limit=limit)


def alive(row):
    """核对后台进程归属；参数：派发；返回：真、假或无法核实。"""
    return owner_alive(row.get('owner_pid'),row.get('owner_process_started_at'))


def launch(settings, identifier, claim_token):
    """启动无终端后台进程；参数：配置、派发编号；返回：进程对象，不执行浏览器逻辑。"""
    directory(settings).mkdir(parents=True,exist_ok=True)
    environment=os.environ.copy()
    environment['ECOMMERCE_JOB_BOOTSTRAP']=json.dumps({'mysql':asdict(settings.mysql),'root':str(settings.root),'workspace':str(settings.workspace)})
    log=directory(settings)/f'{identifier}.log'
    with log.open('ab') as output:
        return subprocess.Popen([sys.executable,'-m','ecommerce.modules.collection.web_worker',
                                 str(settings.workspace.resolve()),identifier,claim_token],env=environment,
                                cwd=settings.root,stdin=subprocess.DEVNULL,stdout=output,stderr=output,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                                start_new_session=os.name!='nt')
