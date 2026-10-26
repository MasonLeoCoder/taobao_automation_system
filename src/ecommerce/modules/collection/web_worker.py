"""网页派发的独立后台进程入口；不启动第二个 Web 服务。"""
from pathlib import Path
import time
import os
import sys

from ecommerce.infrastructure.tasks import web_jobs as jobs
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.infrastructure.tasks.ownership import process_started_at
from ecommerce.infrastructure.tasks.events import sanitize
from ecommerce.modules.collection.web_collection import bound_job,run_collection,validate,saved_result
from ecommerce.shared.config import Settings,MySQLSettings
from ecommerce.shared.exceptions import TaskBusy


def execute(workspace, identifier):
    """执行持久派发并记录可核对结果；参数：工作空间和派发编号；返回：无。"""
    bootstrap=Settings(Path(workspace).parent,Path(workspace),Path(workspace)/'unused-session',Path(workspace)/'unused-profile')
    row=jobs.read(bootstrap,identifier)
    values=dict(row['config'])
    for key in ('root','workspace','session_path','profile_path','taobao_session_path','taobao_profile_path'):
        if values.get(key) is not None:
            values[key]=Path(values[key])
    values['mysql']=MySQLSettings(**values['mysql'])
    settings=Settings(**values)
    if settings.workspace.resolve()!=Path(workspace).resolve():
        raise ValueError('派发工作空间不匹配')
    # 父进程派发时也持有短锁；仅安全重试领取，不重试采集动作。
    for attempt in range(3):
        try:
            with shop_lock(jobs.directory(settings)):
                row=bound_job(settings,row['product_id'],identifier)
                if row['state']!='queued':
                    return
                row.update(state='running',owner_pid=os.getpid(),owner_process_started_at=process_started_at(os.getpid()),
                           message='正在采集当前商品的所选模块')
                jobs.save(settings,row)
            break
        except TaskBusy:
            if attempt==2:
                raise
            time.sleep(.2*(attempt+1))
    try:
        validate(settings,row['product_id'],None if row.get('mode')=='remaining' else row['module'])
        result=run_collection(settings,row)
        row.update(state=result.status,message=sanitize(result.message))
        # 先记执行器结论，再核对页面结果；断库不能把已完成业务谎报为采集失败。
        row['task_status']=result.status
        row['state']='verification_pending'
        jobs.save(settings,row)
        # 查询的是已发布、已确认的本任务结果；旧任务和其他模块不能冒充本次成功。
        row['result']=saved_result(settings,row)
        row['state']=result.status
        if result.status=='succeeded' and not row['result']['valid']:
            row.update(state='needs_attention',message='任务已结束，但未确认本模块有效结果；请查看采集尝试或任务日志')
    except Exception as error:
        row.update(state='verification_pending' if row.get('task_status') else 'failed',
                   message=('任务已结束，资料结果待数据库核对：' if row.get('task_status') else '')+sanitize(str(error)))
    jobs.save(settings,row)


if __name__=='__main__':
    execute(sys.argv[1],sys.argv[2])
