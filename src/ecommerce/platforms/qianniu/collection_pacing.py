"""千牛只读采集共用节奏及平台限流冷却。"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from email.utils import parsedate_to_datetime
import json
from logging import getLogger
from random import uniform
from time import monotonic, time
from urllib.parse import urlsplit

from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.shared.time import BEIJING

_ACTIVE = ContextVar('qianniu_collection_pacing', default=None)
_PLATFORM_HOSTS = {'qn.taobao.com', 'upload.taobao.com', 'item.upload.taobao.com', 'sycm.taobao.com'}


def _path(settings):
    return settings.profile_path / '.collection-cooldown.json'


def _blocked(until):
    deadline = datetime.fromtimestamp(until, BEIJING).strftime('%m-%d %H:%M:%S')
    return NeedsAttention(f'平台暂时限制访问或服务不可用，本次已暂停。请在北京时间 {deadline} 后手动重试')


def check_cooldown(settings):
    path = _path(settings)
    if path.is_file():
        until = json.loads(path.read_text(encoding='utf-8'))['until']
        if until > time():
            raise _blocked(until)


def _retry_deadline(header, now):
    if header and header.strip().isascii() and header.strip().isdigit() and len(header.strip()) < 10:
        return now + max(1, int(header.strip()))
    if header:
        try:
            deadline = parsedate_to_datetime(header)
            if deadline.tzinfo is not None:
                return max(now + 1, deadline.timestamp())
        except (TypeError, ValueError, OverflowError):
            pass
    return now + 60


class _Pacing:
    def __init__(self, settings):
        self.settings = settings
        self.blocked_until = None
        self.saved = False

    def response(self, response):
        status = response.status
        if status not in {403, 429, 503} or urlsplit(response.url).hostname not in _PLATFORM_HOSTS:
            return
        resource = response.request.resource_type
        if resource not in {'document', 'xhr', 'fetch'}:
            return
        retry = response.headers.get('retry-after')
        until = _retry_deadline(retry, time())
        self.blocked_until = max(self.blocked_until or 0, until)

    def check(self):
        if self.blocked_until is None:
            return
        if not self.saved:
            atomic_json(_path(self.settings), {'until': self.blocked_until})
            self.saved = True
        raise _blocked(self.blocked_until)


@contextmanager
def collection_pacing(settings, context):
    """只监听本次采集会话的相关平台响应，不额外请求平台。"""
    pacing = _Pacing(settings)
    context.on('response', pacing.response)
    token = _ACTIVE.set(pacing)
    try:
        yield
        pacing.check()
    finally:
        try:
            context.remove_listener('response', pacing.response)
        finally:
            _ACTIVE.reset(token)


def checkpoint_rate():
    if pacing := _ACTIVE.get():
        pacing.check()


def pause(page, action):
    """页面就绪后按项目动作间隔等待，期间继续检查验证与频控。"""
    pacing = _ACTIVE.get()
    if not pacing:
        return
    from .verification import checkpoint
    seconds = uniform(*pacing.settings.taobao_timing[action])
    getLogger(__name__).info('千牛采集间隔 %s %.3f 秒', action, seconds)
    remaining = seconds
    while remaining > 0:
        checkpoint('千牛采集间隔')
        started = monotonic()
        page.wait_for_timeout(min(250, remaining * 1000))
        remaining -= max(0, monotonic() - started)
    checkpoint('千牛采集间隔结束')
