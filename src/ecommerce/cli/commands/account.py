"""账号命令适配。"""
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.shared.config import Settings


def execute(settings: Settings, action: str):
    """提交账号任务；参数：配置、动作名；返回：任务结果。"""
    from ecommerce.modules.account import service
    handler = service.login if action == "login" else service.check
    return run_task(settings, f"account.{action}", lambda: handler(settings))
