"""项目日志配置。"""

import logging
import traceback
from datetime import datetime
from ecommerce.shared.time import BEIJING
from ecommerce.shared.config import Settings


class BeijingFormatter(logging.Formatter):
    """终端与文本日志固定显示北京时间，不依赖主机当前时区。"""
    def formatTime(self, record, datefmt=None):
        """格式化事件时间；参数：日志记录、可选格式；返回：北京时间文本。"""
        value = datetime.fromtimestamp(record.created, BEIJING)
        return value.strftime(datefmt) if datefmt else value.strftime('%Y-%m-%d %H:%M:%S')+f',{int(record.msecs):03d}'


class SafeLogFilter(logging.Filter):
    """日志输出前移除常见凭据和 URL 签名参数。"""
    def filter(self,record):
        """处理同一记录一次；参数：记录；返回：允许输出。"""
        from ecommerce.infrastructure.tasks.events import sanitize
        if not getattr(record,'sanitized',False):
            record.msg=sanitize(record.getMessage())
            record.args=()
            if record.exc_info:
                record.exc_text=sanitize(''.join(traceback.format_exception(*record.exc_info)))
                record.exc_info=None
            record.sanitized=True
        return True


def setup_logging(settings: Settings) -> None:
    """配置文件及终端日志；参数：Settings；返回：无。"""
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ecommerce")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    formatter = BeijingFormatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    file_handler=logging.FileHandler(settings.log_dir / "run.log", encoding="utf-8")
    console=logging.StreamHandler()
    console.addFilter(lambda record:not getattr(record,'suppress_console',False))
    handlers=[file_handler,console]
    if settings.task_backend=='mysql':
        from ecommerce.infrastructure.tasks.events import EventLogHandler
        handlers.append(EventLogHandler(settings))
    for handler in handlers:
        handler.addFilter(SafeLogFilter())
        handler.setFormatter(formatter)
        logger.addHandler(handler)
