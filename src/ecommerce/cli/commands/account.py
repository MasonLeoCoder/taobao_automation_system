"""账号命令适配。"""
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.shared.config import Settings
from ecommerce.cli.interaction import prompt_manual_verification, prompt_taobao_attention


def execute(settings: Settings, action: str, platform: str = "qianniu", wait: bool = True):
    """提交账号任务；参数：配置、动作名；返回：任务结果。"""
    if platform == "taobao":
        from ecommerce.modules.account import taobao_service as service
    elif platform == "qianniu":
        from ecommerce.modules.account import service
    else:
        raise ValueError(f"不支持的登录平台：{platform}")
    handler = service.login if action == "login" else service.check
    browser_settings = settings.for_platform(platform)
    # 仍以原有项目锁串行执行，避免不同平台同时恢复同一个任务库的运行记录。
    task_name = f"account.taobao.{action}" if platform == "taobao" else f"account.{action}"
    options = {"wait": wait} if platform == "taobao" and action == "login" else {}
    attention = prompt_taobao_attention if platform == "taobao" else prompt_manual_verification
    return run_task(settings, task_name, lambda: handler(browser_settings, on_attention=None if settings.headless else attention, **options))
