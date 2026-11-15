"""采集使用淘宝买家账号的独立资料和固定身份。"""
from dataclasses import replace
from contextvars import ContextVar
from datetime import datetime
import re

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.browser.session import save_session
from ecommerce.infrastructure.storage.platform_accounts import AccountStore
from ecommerce.infrastructure.tasks.events import current_events, emit_event, storage_checkpoint
from ecommerce.modules.account.managed import account_session_directory
from ecommerce.platforms.taobao.identity import read_buyer_nickname
from ecommerce.platforms.taobao.login import check_login, is_authenticated, is_login_required
from ecommerce.platforms.taobao.session import browser_session, open_browser
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import BuyerUnavailable, NeedsAttention, TaskStopped
from ecommerce.shared.time import BEIJING

_ACTIVE_BUYER = ContextVar('active_buyer', default=None)
MISSING_BUYER = '旧采集申请缺少淘宝账号归属，请选择账号新建采集'


def current():
    return _ACTIVE_BUYER.get()


def collection_browser(settings, on_attention=None):
    """将预检浏览器交给实际采集，旧入口仍按原方式启动。"""
    execution = current()
    if execution is None:
        return browser_session(settings, on_attention=on_attention)
    selected, _, opened = execution
    return browser_session(selected, on_attention=on_attention, opened=opened)


def run(settings, name, action, *, buyer, runner, before_browser=None, **kwargs):
    """统一使用环境资源锁，锁内取得所选账号的独立浏览器配置。"""
    if buyer is None:
        def work_without_buyer():
            if before_browser is not None:
                before_browser()
            return action()
        return runner(settings, name, work_without_buyer if before_browser is not None else action, **kwargs)
    execution = None

    def prepare(resources):
        nonlocal execution
        if before_browser is not None and not before_browser():
            return
        selected, row = resolve(settings, buyer)
        opened = resources.enter_context(open_browser(selected))
        probe(selected, row, opened)
        execution = selected, row, opened

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


def named_account(settings, login_name):
    if login_name:
        for row in AccountStore(settings).list_buyers():
            if row['login_name'] == login_name:
                return row
    raise ValueError('请用 --account 指定已登记的淘宝买家账号完整昵称')


def named_target(settings, login_name):
    row = named_account(settings, login_name)
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


def recheck_login(settings, row, guard):
    """在原浏览器的临时首页复核账号，避免把局部提示直接标成掉线。"""
    try:
        with guard.login_probe_page() as page:
            logged_in = check_login(page, min(settings.timeout_ms, 10_000), stop_on_login_required=True)
            login_required = not logged_in and is_login_required(page)
            nickname = read_buyer_nickname(page) if logged_in else None
    except (NeedsAttention, PlaywrightError):
        login_required, nickname = False, None
    if login_required:
        expired(settings, row)
    if not nickname:
        raise BuyerUnavailable('淘宝登录提示出现后未能复核原账号，本次采集暂停，保留原会话')
    if nickname != row['login_name']:
        raise BuyerUnavailable(f'预期账号「{row["login_name"]}」，复核为「{nickname}」，保留原会话')


def probe(settings, row, opened):
    """在创建实际执行之前识别明确掉线，保留排队申请。"""
    with browser_session(settings, hold_on_attention=False, opened=opened) as (_, page):
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


def authenticate(settings, row, page, *, reuse_home=False):
    """执行前核对完整昵称并等待账号证据写入数据库。"""
    guard = active_guard()
    try:
        if reuse_home and guard is not None:
            guard.checkpoint('核对淘宝登录账号')
        authenticated = reuse_home and is_authenticated(page)
        if not authenticated and not check_login(page, settings.timeout_ms, stop_on_login_required=True):
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
    except BuyerUnavailable as error:
        emit_event('taobao_account_start_failed', str(error))
        raise
    events = current_events()
    if events is None:
        raise NeedsAttention('账号核验记录缺少所属任务，采集未开始')
    save_session(page.context, settings)
    operation = events.operation('event', 'taobao_account_verified', '已核验淘宝买家账号', details={
        'account_id': row['id'], 'login_name': nickname, 'verified_at': datetime.now(BEIJING).isoformat(),
    })
    events.collection_barrier = events.append(operation)
    storage_checkpoint('保存淘宝买家账号核验记录')
    if guard:
        guard.login_mode = False
        guard.on_account_login_required = lambda: recheck_login(settings, row, guard)
    return page
