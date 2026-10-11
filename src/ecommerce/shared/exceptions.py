"""跨模块异常及命令执行状态。"""


class AutomationError(Exception):
    """可展示给操作者的业务错误。"""


class NeedsAttention(AutomationError):
    """任务需要人工处理后重新执行。"""


class TaskBusy(AutomationError):
    """店铺浏览器已被其他进程占用。"""
