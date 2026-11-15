"""采集使用淘宝买家账号的独立资料和固定身份。"""
from dataclasses import replace
from contextvars import ContextVar
from datetime import datetime
import re

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.browser.session import save_session
from ecommerce.infrastructure.storage.platform_accounts import AccountStore
from ecommerce.infrastructure.tasks.events import current_events, storage_checkpoint
from ecommerce.modules.account.managed import account_session_directory
from ecommerce.platforms.taobao.identity import read_buyer_nickname
from ecommerce.platforms.taobao.login import check_login, is_login_required
from ecommerce.platforms.taobao.session import browser_session
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import BuyerUnavailable, NeedsAttention, TaskStopped
from ecommerce.shared.time import BEIJING

_ACTIVE_BUYER = ContextVar('active_buyer', default=None)


def current():
    return _ACTIVE_BUYER.get()


def browser_settings(settings):
    execution = current()
    return execution[0] if execution is not None else settings


def run(settings, name, action, *, buyer, runner, **kwargs):
    """统一使用环境资源锁，锁内取得所选账号的独立浏览器配置。"""
    if buyer is None:
        return runner(settings, name, action, **kwargs)
    execution = None

    def prepare():
        nonlocal execution
        execution = resolve(settings, buyer)
        probe(*execution)

    def work():
        token = _ACTIVE_BUYER.set(execution)
        try:
            return action()
        finally:
            _ACTIVE_BUYER.reset(token)

    return runner(settings, name, work, buyer_scope=buyer, prepare=prepare, **kwargs)


def problem(settings, row):
    if not row['enabled']:
        return '账号已停用'
    if row['login_required'] or not row['session_id']:
        return '待重新登录'
    try:
        directory = account_session_directory(settings.workspace, row['session_id'], platform='taobao')
        available = (directory / 'session.json').is_file()
    except (OSError, ValueError):
        available = False
    if not available:
        return '本机会话缺失，待重新登录'
    return ''


def choices(settings):
    if settings.task_backend != 'mysql':
        return []
    rows = [{'id': row['id'], 'login_name': row['login_name'], 'reason': problem(settings, row)}
            for row in AccountStore(settings).list_buyers()]
    return sorted(rows, key=lambda row: (bool(row['reason']), row['login_name'].casefold()))


def account_row(settings, account_id):
    if not isinstance(account_id, str) or not re.fullmatch('[a-f0-9]{32}', account_id):
        raise ValueError('请先选择淘宝买家账号')
    row = next(iter(AccountStore(settings).list_buyers(account_id)), None)
    if row is None:
        raise BuyerUnavailable('所选淘宝买家账号未登记')
    if reason := problem(settings, row):
        raise BuyerUnavailable(f'{row["login_name"]} · {reason}')
    return row


def target(settings, account_id):
    row = account_row(settings, account_id)
    return {'account_id': row['id'], 'login_name': row['login_name']}


def named_target(settings, login_name):
    row = next((row for row in AccountStore(settings).list_buyers() if row['login_name'] == login_name), None)
    if row is None:
        raise ValueError('请用 --account 指定已登记的淘宝买家账号完整昵称')
    return target(settings, row['id'])


def resolve(settings, selected):
    row = account_row(settings, selected['account_id'])
    if row['login_name'] != selected['login_name']:
        raise BuyerUnavailable('已排队账号的昵称已变更，请确认新昵称后重新提交')
    directory = account_session_directory(settings.workspace, row['session_id'], platform='taobao')
    return replace(settings, profile_path=directory / 'browser', session_path=directory / 'session.json'), row


def expired(settings, row):
    AccountStore(settings).change_state(row, 'require_login')
    raise BuyerUnavailable(f'{row["login_name"]} · 淘宝已要求登录，请重新登录原账号')


def probe(settings, row):
    """在创建实际执行之前识别明确掉线，保留排队申请。"""
    with browser_session(settings, hold_on_attention=False) as (_, page):
        try:
            logged_in = check_login(page, settings.timeout_ms, stop_on_login_required=True)
            login_required = not logged_in and is_login_required(page)
        except (NeedsAttention, PlaywrightError):
            if page.is_closed():
                raise TaskStopped('淘宝浏览器已关闭，采集未开始') from None
            # 暂无法识别或需人工验证时，交给原任务内的守卫处理。
            return
        if login_required:
            expired(settings, row)


def authenticate(settings, row, page):
    """执行前核对完整昵称并等待账号证据写入数据库。"""
    if not check_login(page, settings.timeout_ms, stop_on_login_required=True):
        if is_login_required(page):
            expired(settings, row)
        raise BuyerUnavailable('暂未能核对淘宝登录账号，请检查页面后用原账号重试')
    try:
        nickname = read_buyer_nickname(page)
    except PlaywrightError:
        nickname = None
    if not nickname or nickname != row['login_name']:
        actual = f'实际为「{nickname}」' if nickname else '未读到完整昵称'
        raise BuyerUnavailable(f'预期账号「{row["login_name"]}」，{actual}，采集未开始')
    events = current_events()
    if events is None:
        raise NeedsAttention('账号核验记录缺少所属任务，采集未开始')
    save_session(page.context, settings)
    operation = events.operation('event', 'taobao_account_verified', '已核验淘宝买家账号', details={
        'account_id': row['id'], 'login_name': nickname, 'verified_at': datetime.now(BEIJING).isoformat(),
    })
    events.collection_barrier = events.append(operation)
    storage_checkpoint('保存淘宝买家账号核验记录')
    guard = active_guard()
    if guard:
        guard.login_mode = False
        guard.on_account_login_required = lambda: expired(settings, row)
    return page
