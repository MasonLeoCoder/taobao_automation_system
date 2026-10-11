"""所有浏览器任务共用的同步执行入口。"""

import logging
from dataclasses import dataclass
from collections.abc import Callable
from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.shared.config import Settings
from ecommerce.shared.exceptions import NeedsAttention, TaskBusy


@dataclass(frozen=True)
class TaskResult:
    """统一任务返回值，供命令行和未来 Web 任务入口使用。"""

    task_id: str | None
    status: str
    message: str
    exit_code: int


def run_task(settings: Settings, name: str, action: Callable[[], str]) -> TaskResult:
    """记录并执行任务；参数：配置、任务名、业务回调；返回：TaskResult。"""
    database.initialize(settings.database)
    task_id = None
    try:
        with shop_lock(settings.profile_path):
            database.recover_interrupted(settings.database)
            task_id = database.create_task(settings.database, name)
            try:
                message = action()
                status, code = "succeeded", 0
            except (KeyboardInterrupt, EOFError):
                status, code, message = "cancelled", 130, "任务已取消"
            except NeedsAttention as error:
                status, code, message = "needs_attention", 3, str(error)
            except Exception as error:
                logging.getLogger(__name__).exception("任务失败：%s", name)
                status, code, message = "failed", 1, str(error)
            database.finish_task(settings.database, task_id, status, message)
            return TaskResult(task_id, status, message, code)
    except TaskBusy as error:
        return TaskResult(None, "needs_attention", str(error), 3)
