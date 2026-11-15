"""淘宝买家端账号登记和独立会话核验。"""
from dataclasses import replace
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from ecommerce.infrastructure.browser.session import save_session
from ecommerce.infrastructure.storage.platform_accounts import AccountStore
from ecommerce.platforms.taobao.identity import read_buyer_nickname
from ecommerce.platforms.taobao.login import check_login, is_login_required, login_manually
from ecommerce.platforms.taobao.session import browser_session
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped
from .web_accounts import AccountWorkbench


class BuyerAccountWorkbench(AccountWorkbench):
    """使用买家账号的独立目录处理扫码、检查和清理。"""
    platform = 'taobao'
    platform_label = '淘宝买家'

    def accounts(self):
        return AccountStore(self.settings).list_buyers()

    def _account(self, account_id, shop_id, revision):
        current = next(iter(AccountStore(self.settings).list_buyers(account_id)), None)
        if current is None:
            raise LookupError('找不到这个淘宝买家账号，请刷新列表')
        if current['revision'] != revision:
            raise NeedsAttention('账号状态已变化，请刷新列表后重试')
        return current

    def _attention(self, message):
        if message.startswith('【需要人工决定原生弹窗】'):
            raise NeedsAttention('淘宝出现需要确认的浏览器弹窗，本次核验已停止，请重新操作')
        super()._attention(message)
        return '继续'

    def _inspect(self, action, current):
        store = AccountStore(self.settings)
        existing = action == 'check'
        session_id = current['session_id'] if existing else uuid4().hex
        if session_id is None:
            raise NeedsAttention('本机登录已清除，请重新登录')
        directory = self._profile(session_id)
        settings = replace(self.settings, profile_path=directory / 'browser', session_path=directory / 'session.json')
        if existing and not settings.session_path.is_file():
            store.change_state(current, 'require_login')
            self._update(refresh_accounts=True)
            raise NeedsAttention('未找到账号的本机会话，请重新登录')
        keep_directory = existing
        try:
            with browser_session(settings, on_attention=None if settings.headless else self._attention,
                                 hold_on_attention=False) as (context, page):
                if existing:
                    if not check_login(page, settings.timeout_ms, stop_on_login_required=True):
                        if is_login_required(page):
                            store.change_state(current, 'require_login')
                            self._update(refresh_accounts=True)
                            raise NeedsAttention('淘宝已明确要求登录，请重新扫码')
                        raise NeedsAttention('本次核验未完成，请检查网络或淘宝页面后重试')
                else:
                    self._update(phase='scan', message='请在淘宝窗口扫码，登录后会核对账号菜单里的完整昵称')
                    page = login_manually(page, settings.timeout_ms, on_wait=self._check_cancelled)
                    with self.lock:
                        if self.cancelled:
                            raise TaskStopped('已取消本次操作')
                        self.state.update(phase='running', message='正在核对淘宝完整昵称')
                nickname = read_buyer_nickname(page)
                if not nickname:
                    raise NeedsAttention('本次核验未完成，未读到淘宝账号菜单里的完整昵称')
                if current and nickname != current['login_name']:
                    raise NeedsAttention(f'预期账号「{current["login_name"]}」，实际登录「{nickname}」，保留原会话')
                if not existing:
                    save_session(context, settings)
            # 数据库可能已提交后才断开连接，此时保留新目录，让账号页可据数据库状态核对。
            keep_directory = True
            try:
                store.record_buyer(nickname, session_id, current=current)
            except NeedsAttention:
                keep_directory = existing
                raise
        except PlaywrightTimeoutError:
            raise NeedsAttention('本次核验未完成，淘宝页面加载超时，请检查网络后重试') from None
        except PlaywrightError:
            raise NeedsAttention('本次核验未完成，淘宝窗口已关闭或页面访问中断') from None
        finally:
            if not keep_directory:
                self._remove_session(session_id)
        self._update(refresh_accounts=True)
        if action == 'login' and store.cleanup_sessions(current['id'], self._remove_session):
            return f'已核验「{nickname}」，旧登录资料待清理', True
        return f'已核验淘宝买家账号「{nickname}」', False
