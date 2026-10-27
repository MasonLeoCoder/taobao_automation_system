"""读取MySQL申请的独立执行进程；失效领取不能打开浏览器。"""
from pathlib import Path
import json
import logging
import os
import sys
from ecommerce.infrastructure.tasks.ownership import process_started_at
from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
from ecommerce.modules.collection.web_collection import run_collection, validate_job
from ecommerce.shared.config import Settings, MySQLSettings


def settings_from_payload(values):
    """还原固定配置，不读取旧派发文件；参数：数据库配置快照；返回：配置。"""
    values=dict(values)
    for key in ('root','workspace','session_path','profile_path','taobao_session_path','taobao_profile_path','qianniu_session_path','qianniu_profile_path'):
        if values.get(key) is not None:
            values[key]=Path(values[key])
    values['mysql']=MySQLSettings(**values['mysql'])
    return Settings(**values)


def execute(workspace, identifier, claim_token, *, bootstrap=None):
    """核对领取后交给统一执行器；参数：工作空间、申请、令牌、引导配置；返回：无。"""
    if bootstrap is None:
        values=json.loads(os.environ['ECOMMERCE_JOB_BOOTSTRAP'])
        bootstrap=Settings(Path(values['root']),Path(values['workspace']),Path(workspace)/'unused-session',
            Path(workspace)/'unused-profile',mysql=MySQLSettings(**values['mysql']),task_backend='mysql',collection_backend='mysql')
    persistence=CollectionJobStore(bootstrap)
    row=persistence.read(identifier)
    if row.get('claim_token')!=claim_token or row['dispatch_state']!='claimed' or row['stop_requested']:
        return
    settings=settings_from_payload(row['config'])
    if settings.workspace.resolve()!=Path(workspace).resolve() or settings.mysql!=bootstrap.mysql:
        raise ValueError('采集申请工作空间或数据库不匹配')
    if not persistence.bind_process(identifier,claim_token,os.getpid(),process_started_at(os.getpid()),replace_owner=True):
        return
    try:
        validate_job(settings,row)
        result=run_collection(settings,row)
        if result.task_id is None:
            persistence.fail_start(identifier,claim_token,result.message)
        from ecommerce.modules.collection.dispatch import after_execution
        after_execution(settings,identifier,result.status)
    except Exception:
        logging.getLogger(__name__).exception('采集进程退出，执行记录及待补写流水保留')
        from ecommerce.modules.collection.dispatch import pause_queue
        pause_queue(settings,'采集进程异常，等待核对后继续')
        try:
            persistence.fail_start(identifier,claim_token,'后台启动或配置核对失败，未进入采集')
        except Exception:
            logging.getLogger(__name__).exception('启动状态待数据库恢复核对')


if __name__=='__main__':
    execute(sys.argv[1],sys.argv[2],sys.argv[3])
