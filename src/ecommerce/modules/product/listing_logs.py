"""上架任务日志查询：复用任务事件，文件后端保留同任务日志。"""
from contextlib import contextmanager
import json
import logging
import threading

from ecommerce.infrastructure.storage.database import now
from ecommerce.infrastructure.tasks.events import sanitize

PAGE_SIZE = 100
MAX_CURSOR = 2**63 - 1


def _entry(number, time, level, message, step=None):
    """构造安全网页摘要；参数：编号、时间、级别、消息及步骤；返回：单条日志。"""
    return {'id': number, 'time': time, 'level': level, 'step': sanitize(step or '')[:250],
            'message': sanitize(message or '')[:4500]}


def log_page(settings, path, task_id, *, after=None, before=None):
    """按本次任务游标读取日志；参数：环境、导入目录、任务编号、游标；返回：事件及翻页边界。"""
    if task_id and settings.task_backend == 'mysql':
        data = settings.task_database.list_events(task_id=task_id, after=after or 0,
            before=(before or MAX_CURSOR) if after is None else None, limit=PAGE_SIZE)
        rows = [_entry(row['task_sequence'], row['occurred_at'], row['level'], row['message'], row['step'])
                for row in data['events']]
        previous, more = data['has_previous'], data['has_more']
    else:
        file = path / 'task-log.jsonl'
        rows = []
        if task_id and file.exists():
            with file.open(encoding='utf-8') as stream:
                for line in stream:
                    # 读取与追加并行时不消费尚未写完整的最后一条。
                    if line.endswith('\n'):
                        rows.append(json.loads(line))
        if after is not None:
            previous = bool(rows and rows[0]['id'] <= after)
            newer = [row for row in rows if row['id'] > after]
            rows, more = newer[:PAGE_SIZE], len(newer) > PAGE_SIZE
        else:
            older = [row for row in rows if row['id'] < (before or MAX_CURSOR)]
            previous, more = len(older) > PAGE_SIZE, False
            rows = older[-PAGE_SIZE:]
    return {'events': rows, 'next_cursor': rows[-1]['id'] if rows else (after or 0),
            'previous_cursor': rows[0]['id'] if rows else None, 'has_previous': previous, 'has_more': more}


class _LocalLog(logging.Handler):
    """文件后端只采当前执行线程；正式和测试 MySQL 继续使用公共任务事件。"""

    def __init__(self, path):
        super().__init__()
        self.path = path / 'task-log.jsonl'
        self.owner = threading.get_ident()
        self.sequence = 0

    def write(self, message, level='INFO', step=None):
        """顺序追加一条任务摘要；参数：消息、级别、步骤；返回：无。"""
        self.sequence += 1
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(_entry(self.sequence, now(), level, message, step), ensure_ascii=False) + '\n')

    def emit(self, record):
        """仅转发该任务产生的日志；参数：日志记录；返回：无。"""
        if threading.get_ident() != self.owner:
            return
        try:
            self.write(record.getMessage(), record.levelname)
        except (OSError, ValueError):
            self.handleError(record)


@contextmanager
def local_log(settings, path):
    """在文件任务期间捕获日志；参数：配置和导入目录；返回：本地记录器或空值。"""
    if settings.task_backend == 'mysql':
        yield None
        return
    handler = _LocalLog(path)
    logger = logging.getLogger('ecommerce')
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        handler.close()
