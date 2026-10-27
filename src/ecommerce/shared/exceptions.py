"""跨模块异常及命令执行状态。"""


class AutomationError(Exception):
    """可展示给操作者的业务错误。"""


class NeedsAttention(AutomationError):
    """任务需要人工处理后重新执行。"""


class TaskBusy(AutomationError):
    """店铺浏览器已被其他进程占用。"""


class TaskStopped(AutomationError):
    """用户明确停止任务；保存已有进度后退出，不视为成功。"""


class ModuleBlocked(AutomationError):
    """单一采集模块无法推进，保存部分结果后跳过，不影响整批。"""


class BrowserClosed(TaskStopped):
    """业务使用的采集页或浏览器被关闭，停止整次执行及补采队列。"""
