"""账号命令适配。"""
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.modules.account.managed import account_target, registered_account, selected_account
from ecommerce.shared.config import Settings
from ecommerce.cli.interaction import prompt_manual_verification, prompt_taobao_attention


def execute(settings: Settings, action: str, platform: str = "qianniu", wait: bool = True, *,
            shop_id=None, account=None):
    """提交账号任务；参数：配置、动作名；返回：任务结果。"""
    if platform == "taobao":
        from ecommerce.modules.account import taobao_service as service
    elif platform == "qianniu":
        return _managed(settings, action, shop_id, account)
    else:
        raise ValueError(f"不支持的登录平台：{platform}")
    handler = service.login if action == "login" else service.check
    browser_settings = settings.for_platform(platform)
    # 仍以原有项目锁串行执行，避免不同平台同时恢复同一个任务库的运行记录。
    options = {"wait": wait} if action == "login" else {}
    return run_task(settings, f"account.taobao.{action}",
        lambda: handler(browser_settings, on_attention=None if settings.headless else prompt_taobao_attention, **options))


def _managed(settings, action, shop_id, account):
    from ecommerce.modules.account.web_accounts import AccountWorkbench
    from ecommerce.shared.exceptions import NeedsAttention

    settings = settings.for_platform('qianniu')
    relogin = action == 'login'
    if relogin and settings.headless:
        raise NeedsAttention('千牛扫码需要可见浏览器，请关闭 headless 后重试')
    target = account_target(registered_account(settings, shop_id, account, allow_relogin=relogin))

    def work():
        row = selected_account(settings, target['account_id'], target['shop_id'], allow_relogin=relogin)
        workbench = AccountWorkbench(settings, headless=settings.headless)
        return workbench.inspect_selected(action, row,
            on_attention=None if settings.headless else prompt_manual_verification,
            on_scan=lambda _page, **_: print(f'请在千牛窗口为 {target["login_name"]} 扫码。', flush=True))

    return run_task(settings, f'account.{action}', work, qianniu_scope=target)
