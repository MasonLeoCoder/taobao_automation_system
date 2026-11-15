"""所有浏览器任务共用的同步执行入口。"""

import logging
from contextlib import ExitStack
from dataclasses import dataclass
from collections.abc import Callable
from contextvars import ContextVar
from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.shared.config import Settings
from ecommerce.shared.exceptions import BuyerUnavailable, NeedsAttention, TaskBusy, TaskStopped

_CURRENT_TASK = ContextVar("current_ecommerce_task", default=None)


def current_task():
    """返回当前任务存储入口和编号，供持久进度记录使用。"""
    return _CURRENT_TASK.get()


@dataclass(frozen=True)
class TaskResult:
    """统一任务返回值，供命令行和未来 Web 任务入口使用。"""

    task_id: str | None
    status: str
    message: str
    exit_code: int
    account_blocked: bool = False


def run_task(settings: Settings, name: str, action: Callable[[], str], *, task_id=None, job_claim_token=None, qianniu_scope=None, buyer_scope=None, prepare=None) -> TaskResult:
    """记录并执行任务；参数：配置、任务名、业务回调；返回：TaskResult。"""
    if settings.task_backend=='mysql':
        if buyer_scope is not None:
            return _run_mysql_task(settings, name, action, task_id=task_id, job_claim_token=job_claim_token,
                                   buyer_scope=buyer_scope, prepare=prepare)
        if qianniu_scope is not None:
            return _run_mysql_task(settings, name, action, task_id=task_id, qianniu_scope=qianniu_scope)
        return _run_mysql_task(settings,name,action,task_id=task_id,job_claim_token=job_claim_token) if task_id else _run_mysql_task(settings,name,action)
    if task_id is not None or qianniu_scope is not None or buyer_scope is not None:
        raise ValueError('预分配任务编号和千牛账号归属需要启用 MySQL')
    database.initialize(settings.task_database)
    task_id = None
    try:
        with shop_lock(settings.profile_path):
            database.recover_interrupted(settings.database)
            task_id = database.create_task(settings.database, name)
            token = _CURRENT_TASK.set((settings.database, task_id))
            try:
                message = action()
                status, code = "succeeded", 0
            except (KeyboardInterrupt, EOFError):
                status, code, message = "cancelled", 130, "任务已取消"
            except TaskStopped as error:
                status, code, message = "cancelled", 130, str(error)
            except NeedsAttention as error:
                status, code, message = "needs_attention", 3, str(error)
            except Exception as error:
                logging.getLogger(__name__).exception("任务失败：%s", name)
                status, code, message = "failed", 1, str(error)
            finally:
                _CURRENT_TASK.reset(token)
            database.finish_task(settings.database, task_id, status, message)
            return TaskResult(task_id, status, message, code)
    except TaskBusy as error:
        return TaskResult(None, "needs_attention", str(error), 3)


def _run_mysql_task(settings,name,action,*,task_id=None,job_claim_token=None,qianniu_scope=None,buyer_scope=None,prepare=None):
    """执行已确认入库的任务，保留未提交结论；参数：配置、名称、业务；返回：真实任务结果。"""
    from ecommerce.infrastructure.tasks.events import TaskEvents,event_session,replay_events
    from ecommerce.infrastructure.tasks.ownership import atomic_json,execution_key,owner_alive
    from ecommerce.infrastructure.storage.mysql import MySQLStorageError
    store=settings.task_database
    session=None
    account_blocked=False
    prepared = prepare is None
    try:
        store.initialize()
        with shop_lock(settings.profile_path), ExitStack() as resources:
            replay_events(settings,store)
            store.recover_interrupted(execution_key(settings),owner_alive)
            if qianniu_scope is not None and task_id and store.get_task(task_id) is not None:
                raise TaskBusy('此请求已有任务，请查看原采集记录')
            session=TaskEvents(settings,store,name,task_id=task_id) if task_id else TaskEvents(settings,store,name)
            if qianniu_scope is not None:
                session.owner.update(account_id=qianniu_scope['account_id'], shop_id=qianniu_scope['shop_id'])
            elif buyer_scope is not None:
                session.owner.update(account_id=buyer_scope['account_id'])
            session.expected_job_claim=job_claim_token
            if prepare is not None:
                if job_claim_token:
                    from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
                    pending = CollectionJobStore(settings).read(task_id)
                    if (pending['dispatch_state'] != 'claimed' or pending['claim_token'] != job_claim_token
                            or pending['stop_requested']):
                        raise TaskStopped('采集领取已失效，未打开浏览器')
                atomic_json(session.active_path,session.owner)
                prepare(resources)
                prepared = True
            token=_CURRENT_TASK.set((store,session.task_id))
            try:
                with event_session(session):
                    if qianniu_scope is not None:
                        session.start(details={'qianniu_source': qianniu_scope})
                    elif buyer_scope is not None:
                        session.start(details={'taobao_requested': buyer_scope})
                    else:
                        session.start()
                    try:
                        message=action()
                        status,code='succeeded',0
                    except (KeyboardInterrupt,EOFError):
                        status,code,message='cancelled',130,'任务已取消'
                    except TaskStopped as error:
                        status,code,message='cancelled',130,str(error)
                    except NeedsAttention as error:
                        status,code,message='needs_attention',3,str(error)
                        account_blocked=isinstance(error,BuyerUnavailable)
                    except Exception as error:
                        logging.getLogger(__name__).exception('任务失败：%s',name)
                        status,code,message='failed',1,str(error)
                    finished = session.finish(status,message,account_blocked=True) if account_blocked else session.finish(status,message)
                    if not finished:
                        return TaskResult(session.task_id,'needs_attention',f'业务实际结果：{status}；结果待入库，已保留本地流水：{session.journal.path}',3)
                    return TaskResult(session.task_id,status,message,code,account_blocked)
            finally:
                _CURRENT_TASK.reset(token)
    except TaskStopped as error:
        if session and getattr(session,'started',False):
            session.finish('cancelled',str(error))
        return TaskResult(session.task_id if session and getattr(session,'started',False) else None,'cancelled',str(error),130)
    except (TaskBusy,MySQLStorageError,NeedsAttention) as error:
        logging.getLogger(__name__).error('任务未继续执行：%s',error)
        return TaskResult(session.task_id if session and prepared else None,'needs_attention',str(error),3,isinstance(error,BuyerUnavailable))
    finally:
        if session:
            session.close()
            if getattr(session,'job_origin',None)=='cli' and locals().get('finished'):
                try:
                    from ecommerce.modules.collection.dispatch import after_execution
                    after_execution(settings,session.task_id,locals().get('status','needs_attention'),
                                    **({'account_blocked':True} if account_blocked else {}))
                except Exception:
                    logging.getLogger(__name__).exception('补采队列状态待核对；不会重放采集动作')
