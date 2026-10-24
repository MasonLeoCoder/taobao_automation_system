"""所有浏览器任务共用的同步执行入口。"""

import logging
from dataclasses import dataclass
from collections.abc import Callable
from contextvars import ContextVar
from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.shared.config import Settings
from ecommerce.shared.exceptions import NeedsAttention, TaskBusy, TaskStopped

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


def run_task(settings: Settings, name: str, action: Callable[[], str]) -> TaskResult:
    """记录并执行任务；参数：配置、任务名、业务回调；返回：TaskResult。"""
    if settings.task_backend=='mysql':
        return _run_mysql_task(settings,name,action)
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


def _run_mysql_task(settings,name,action):
    """执行已确认入库的任务，保留未提交结论；参数：配置、名称、业务；返回：真实任务结果。"""
    from ecommerce.infrastructure.tasks.events import TaskEvents,event_session,replay_events
    from ecommerce.infrastructure.tasks.ownership import execution_key,owner_alive
    from ecommerce.infrastructure.storage.mysql import MySQLStorageError
    store=settings.task_database
    session=None
    try:
        store.initialize()
        with shop_lock(settings.profile_path):
            replay_events(settings,store)
            store.recover_interrupted(execution_key(settings),owner_alive)
            session=TaskEvents(settings,store,name)
            token=_CURRENT_TASK.set((store,session.task_id))
            try:
                with event_session(session):
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
                    except Exception as error:
                        logging.getLogger(__name__).exception('任务失败：%s',name)
                        status,code,message='failed',1,str(error)
                    if not session.finish(status,message):
                        return TaskResult(session.task_id,'needs_attention',f'业务实际结果：{status}；结果待入库，已保留本地流水：{session.journal.path}',3)
                    return TaskResult(session.task_id,status,message,code)
            finally:
                _CURRENT_TASK.reset(token)
    except (TaskBusy,MySQLStorageError,NeedsAttention) as error:
        logging.getLogger(__name__).error('任务未继续执行：%s',error)
        return TaskResult(session.task_id if session else None,'needs_attention',str(error),3)
    finally:
        if session:
            session.close()
