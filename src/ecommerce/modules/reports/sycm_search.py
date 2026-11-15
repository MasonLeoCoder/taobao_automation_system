"""按批执行生意参谋搜索排行采集。"""
from dataclasses import replace
from logging import getLogger
from threading import Event, Lock, Thread
from uuid import uuid4

from ecommerce.infrastructure.storage import sycm_search as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.tasks.events import current_events
from ecommerce.infrastructure.tasks.runner import current_task, run_task
from ecommerce.modules.account.managed import (
    account_settings, account_target, require_login, selected_account, verify_session,
)
from ecommerce.platforms.qianniu.collection_pacing import check_cooldown, collection_pacing
from ecommerce.platforms.qianniu.session import browser_session
from ecommerce.platforms.sycm import search_rank
from ecommerce.shared.exceptions import NeedsAttention, TaskBusy


class SearchRankingWorkbench:
    def __init__(self, settings):
        self.settings = replace(settings.for_platform('qianniu'), headless=False)
        self.lock = Lock()
        self.feedback = Event()
        self.state = None

    def snapshot(self, request_id=None):
        with self.lock:
            if self.state and (request_id is None or self.state['id'] == request_id):
                return dict(self.state)
        return repository.request_status(self.settings, request_id) if request_id else None

    def _update(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self, category, requested_path, period, pages, word_types, *, account_id, shop_id, request_id):
        request_hash = fingerprint([category['id'], requested_path, period, pages, word_types, shop_id, account_id])
        with self.lock:
            existing = self.state if self.state and self.state['id'] == request_id else repository.request_status(
                self.settings, request_id)
            if existing:
                if existing['target'].get('request_hash') != request_hash:
                    raise ValueError('采集请求编号已用于其他设置，请重新发起')
                return dict(existing)
            if self.state and self.state['phase'] in {'queued', 'running', 'waiting'}:
                raise TaskBusy('已有搜索排行任务正在运行')
            target = dict(account_target(selected_account(self.settings, account_id, shop_id)), request_hash=request_hash)
            self.state = {'id': request_id, 'task_id': None, 'category_id': category['id'], 'period': period,
                          'phase': 'queued', 'prompt': None, 'message': '等待千牛会话',
                          'target': target, 'has_batch': False}
            try:
                Thread(target=self._run, args=(request_id, category, requested_path, period, pages, word_types, target),
                       daemon=True, name='sycm-search').start()
            except RuntimeError:
                self.state.update(phase='failed', message='搜索排行任务启动失败')
                raise
            return dict(self.state)

    def resume(self, prompt):
        with self.lock:
            if not self.state or self.state['phase'] != 'waiting' or self.state['prompt'] != prompt:
                raise ValueError('处理提示已变化，请刷新页面')
            self.state.update(phase='running', prompt=None, message='正在复查生意参谋页面')
            self.feedback.set()

    def _attention(self, message):
        self.feedback.clear()
        self._update(phase='waiting', prompt=uuid4().hex, message=message)
        if events := current_events():
            events.progress('needs_attention', message)
        if not self.feedback.wait(600):
            raise NeedsAttention('等待人工处理超时，请重采所需词类型')
        if events:
            events.progress('running', '正在复查当前页面')

    def _run(self, identifier, category, requested_path, period, pages, word_types, target):
        label = repository.PERIODS[period]

        def work():
            task_id = current_task()[1]
            self._update(phase='running', task_id=task_id, message='正在检查千牛登录')
            type_ids = repository.begin(self.settings, identifier, task_id, category, period, pages, word_types)
            self._update(has_batch=True)
            try:
                check_cooldown(self.settings)
                row = selected_account(self.settings, target['account_id'], target['shop_id'])
                browser_settings = account_settings(self.settings, row)

                def expired():
                    require_login(self.settings, row)

                try:
                    with browser_session(browser_settings, on_attention=self._attention) as (context, page), collection_pacing(self.settings, context):
                        verify_session(page, target, expired)
                        self._update(message='正在核对生意参谋统计类目和日期')
                        path, platform_id, start, end = search_rank.prepare(page, requested_path, period, label)
                        repository.confirm_scope(self.settings, identifier, path, platform_id, start, end)
                        scope = (path, period, label, (start, end))
                        for word_type, type_id in type_ids.items():
                            word_label = repository.WORD_TYPES[word_type]
                            self._update(message=f'正在采集{word_label}第 1 页')

                            def save(number, entries):
                                repository.save_page(self.settings, type_id, number, entries, database_now())
                                self._update(message=f'{word_label}已保存 {number} 页，正在核对后续数据')

                            reason, failed_page, error = search_rank.collect_type(page, scope, word_label, pages, save)
                            repository.finish_type(self.settings, type_id, reason, failed_page, error)
                except search_rank.RankingLoginRequired:
                    expired()
            except Exception as error:
                repository.finish(self.settings, identifier, str(error).splitlines()[0])
                if isinstance(error, search_rank.RankingError):
                    raise NeedsAttention(str(error)) from error
                raise
            status = repository.finish(self.settings, identifier)
            if status == 'failed':
                raise NeedsAttention('未采到有效排行，请在采集记录查看原因')
            return '搜索排行已保存' if status == 'complete' else '已保存部分排行，请在采集记录查看未完成项'

        try:
            result = run_task(self.settings, 'sycm.search_rank', work, task_id=identifier, qianniu_scope=target)
            if not result.task_id:
                previous = repository.request_status(self.settings, identifier)
                if previous and previous['target'].get('request_hash') == target['request_hash']:
                    self._update(**previous)
                    return
            self._update(phase=result.status, prompt=None, message=result.message, task_id=result.task_id)
        except Exception:
            getLogger(__name__).exception('生意参谋排行后台执行中断')
            self._update(phase='failed', prompt=None, message='搜索排行任务中断，请查看运行日志')
