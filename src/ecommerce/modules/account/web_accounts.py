"""千牛网页账号登记、会话检查和重新登录。"""

from dataclasses import replace
from logging import getLogger
from threading import Event, Lock, Thread
from uuid import UUID, uuid4
import shutil

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from ecommerce.infrastructure.browser.session import save_session
from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from ecommerce.infrastructure.storage.platform_accounts import AccountStore
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.platforms.qianniu.identity import read_shop_identity
from ecommerce.platforms.qianniu.login import check_login, login_with_qrcode
from ecommerce.platforms.qianniu.session import browser_session
from ecommerce.shared.exceptions import NeedsAttention, TaskBusy, TaskStopped


class AccountWorkbench:
    def __init__(self, settings):
        self.settings = replace(settings.for_platform('qianniu'), headless=False)
        self.root = (self.settings.workspace / 'storage/managed_accounts/qianniu').resolve()
        self.lock = Lock()
        self.feedback = Event()
        self.state = None
        self.cancelled = False

    def accounts(self):
        return AccountStore(self.settings).list_accounts()

    def snapshot(self):
        with self.lock:
            return dict(self.state) if self.state else None

    def _update(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self, action, account_id=None, shop_id=None):
        with self.lock:
            if self.state and self.state['phase'] in {'running', 'scan', 'waiting', 'open', 'stopping'}:
                raise TaskBusy('已有千牛账号检查或扫码正在进行')
            current = None
            if action != 'add':
                current = next((row for row in AccountStore(self.settings).list_accounts(account_id)
                                if row['platform_shop_id'] == shop_id), None)
                if current is None:
                    raise LookupError('找不到这个账号及店铺，请刷新列表')
            self.state = {'id': uuid4().hex, 'phase': 'running', 'prompt': None,
                          'message': '正在打开千牛浏览器'}
            self.cancelled = False
            try:
                Thread(target=self._run, args=(action, current), daemon=True, name='qianniu-accounts').start()
            except RuntimeError:
                self.state.update(phase='failed', message='无法启动千牛浏览器')
                raise
            return dict(self.state)

    def respond(self, identifier, action, prompt):
        with self.lock:
            if not self.state or self.state['id'] != identifier:
                raise ValueError('当前操作已变化，请刷新页面')
            if action == 'cancel' and self.state['phase'] in {'scan', 'waiting', 'open'}:
                self.cancelled = True
                self.state.update(phase='stopping', prompt=None, message='正在取消')
            elif action == 'continue' and self.state['phase'] == 'waiting' and self.state['prompt'] == prompt:
                self.state.update(phase='running', prompt=None, message='正在复查千牛页面')
            else:
                raise ValueError('当前提示已变化，请刷新页面')
            self.feedback.set()

    def _check_cancelled(self):
        with self.lock:
            if self.cancelled:
                raise TaskStopped('已取消本次操作')

    def _attention(self, message):
        self.feedback.clear()
        self._update(phase='waiting', prompt=uuid4().hex, message=message)
        if not self.feedback.wait(600):
            raise NeedsAttention('等待人工处理超时，请重新检查账号')
        self._check_cancelled()

    def _on_scan(self, page, *, retry):
        with self.lock:
            if self.cancelled:
                raise TaskStopped('已取消本次操作')
            self.state.update(phase='scan', message='请在千牛窗口扫码，系统会在登录后核对账号和店铺')

    def _profile(self, identifier):
        if UUID(hex=identifier).hex != identifier:
            raise ValueError('会话编号无效')
        directory = (self.root / identifier).resolve()
        if directory.parent != self.root or directory.name != identifier:
            raise ValueError('会话目录不在千牛账号资料下')
        return directory

    def _remove_session(self, identifier):
        try:
            shutil.rmtree(self._profile(identifier))
        except FileNotFoundError:
            pass
        except OSError:
            getLogger(__name__).warning('千牛旧会话目录暂无法清理')

    def _run(self, action, current):
        try:
            # 沿用原千牛资源锁，账号管理与旧入口及数据库维护串行。
            with shop_lock(self.settings.profile_path):
                message = self._inspect(action, current)
            self._update(phase='succeeded', prompt=None, message=message)
        except (NeedsAttention, TaskBusy, TaskStopped, MySQLStorageError) as error:
            self._update(phase='cancelled' if isinstance(error, TaskStopped) else 'failed',
                         prompt=None, message=str(error))
        except Exception:
            getLogger(__name__).exception('千牛账号操作失败')
            self._update(phase='failed', prompt=None, message='千牛账号操作失败，请查看运行日志')

    def _inspect(self, action, current):
        store = AccountStore(self.settings)
        existing = action in {'check', 'open'}
        session_id = current['session_id'] if existing else uuid4().hex
        directory = self._profile(session_id)
        settings = replace(self.settings, profile_path=directory / 'browser', session_path=directory / 'session.json')
        if existing and not settings.session_path.is_file():
            raise NeedsAttention('未找到账号的本机会话，请重新登录')
        ready_to_publish = False
        try:
            with browser_session(settings, on_attention=self._attention, restore_state=existing) as (context, page):
                if existing:
                    if not check_login(page):
                        raise NeedsAttention('此账号登录已失效，请重新登录')
                else:
                    page = login_with_qrcode(page, on_wait=self._on_scan)
                    with self.lock:
                        if self.cancelled:
                            raise TaskStopped('已取消本次操作')
                        self.state.update(phase='running', message='正在核对完整登录名和店铺')
                identity = read_shop_identity(page)
                if identity is None or not identity.login_name:
                    raise NeedsAttention('未能确认完整登录名和店铺，请核对千牛页面后重试')
                if current and (identity.login_name, identity.shop_id) != (current['login_name'], current['platform_shop_id']):
                    raise NeedsAttention(f'检测到 {identity.login_name} · {identity.shop_name}，与所选账号或店铺不符，保留原会话')
                save_session(context, settings)
                if action == 'open':
                    store.record(identity, session_id, current=current)
                    self._keep_open(context, identity)
                    return f'已关闭 {identity.login_name} 的千牛窗口'
            ready_to_publish = True
            try:
                store.record(identity, session_id, current=current)
            except NeedsAttention:
                ready_to_publish = False
                raise
        except PlaywrightTimeoutError:
            raise NeedsAttention('千牛页面或扫码等待超时，请重试') from None
        finally:
            if not existing and not ready_to_publish:
                self._remove_session(session_id)
        if action == 'login':
            self._remove_session(current['session_id'])
        return f'已核验 {identity.login_name} · {identity.shop_name}'

    def _keep_open(self, context, identity):
        self._update(phase='open', message=f'已打开 {identity.login_name} · {identity.shop_name}')
        while context.pages:
            with self.lock:
                if self.cancelled:
                    return
            page = context.pages[-1]
            try:
                page.wait_for_timeout(300)
            except PlaywrightError:
                if context.pages:
                    raise
