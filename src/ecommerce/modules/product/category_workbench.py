"""千牛类目采集后台入口，复用千牛会话和任务锁。"""
from dataclasses import replace
from logging import getLogger
from threading import Event, Lock, Thread
from uuid import uuid4

from ecommerce.infrastructure.storage import publish_categories as repository
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.modules.account.service import authenticate
from ecommerce.platforms.qianniu.collection_pacing import check_cooldown, collection_pacing, pause
from ecommerce.platforms.qianniu.product import click_product_nav, click_publish_product
from ecommerce.platforms.qianniu.publish_categories import collect_publish_categories
from ecommerce.platforms.qianniu.session import browser_session
from ecommerce.shared.exceptions import NeedsAttention, TaskBusy
from .listing_profiles import INITIAL_PROFILES

PILOT = '宠物/宠物食品及用品'


class CategoryWorkbench:
    def __init__(self, settings):
        self.settings = replace(settings.for_platform('qianniu'), headless=False)
        self.lock = Lock()
        self.state = None
        self.feedback_event = Event()

    def snapshot(self):
        with self.lock:
            return dict(self.state) if self.state else None

    def _update(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self, scopes):
        with self.lock:
            if self.state and self.state['phase'] in {'queued', 'running', 'waiting'}:
                raise TaskBusy('已有类目采集正在运行')
            identifier = uuid4().hex
            self.state = {'id': identifier, 'phase': 'queued', 'prompt': None, 'message': '等待千牛会话'}
            try:
                Thread(target=self._run, args=(identifier, scopes), daemon=True,
                       name='publish-categories').start()
            except RuntimeError:
                self.state.update(phase='failed', message='无法启动类目采集')
                raise
            return dict(self.state)

    def resume(self, prompt):
        with self.lock:
            if not self.state or self.state['phase'] != 'waiting' or self.state['prompt'] != prompt:
                raise ValueError('验证提示已变化，请刷新后检查')
            self.state.update(phase='running', prompt=None, message='正在复查千牛页面')
            self.feedback_event.set()

    def _attention(self, message):
        self.feedback_event.clear()
        self._update(phase='waiting', prompt=uuid4().hex, message=message)
        if not self.feedback_event.wait(600):
            raise NeedsAttention('等待千牛人工反馈超时，请重新采集')

    def _run(self, identifier, scopes):
        def collect():
            check_cooldown(self.settings)
            repository.begin_scan(self.settings, identifier, scopes)
            try:
                self._update(phase='running', message='正在检查千牛登录')
                with browser_session(self.settings, on_attention=self._attention) as (context, page), collection_pacing(self.settings, context):
                    page = authenticate(context, page, self.settings, on_login_wait=lambda _page, **_kwargs:
                        self._attention('请在千牛浏览器完成扫码，再点击「已处理，继续」'))
                    page = click_product_nav(context, page)
                    pause(page, 'page')
                    page = click_publish_product(context, page)
                    pause(page, 'page')
                    observed, complete, failed = collect_publish_categories(page, scopes,
                        on_progress=lambda path: self._update(message=f'正在采集 {path}'))
                status = repository.apply_scan(self.settings, identifier, observed, complete, failed, INITIAL_PROFILES)
            except Exception as error:
                repository.fail_scan(self.settings, identifier, str(error).splitlines()[0])
                raise
            if status == 'failed':
                raise NeedsAttention('类目未能确认，请在采集记录查看原因')
            return '已更新发布类目目录' if status == 'complete' else '部分分支已更新，请查看待重试或待核对项'

        try:
            result = run_task(self.settings, 'product.categories', collect)
            self._update(phase=result.status, prompt=None, message=result.message)
        except Exception:
            getLogger(__name__).exception('类目采集任务中断')
            self._update(phase='failed', prompt=None, message='类目采集中断，请查看运行日志')
