"""千牛业务共用的账号选择、会话与身份核对。"""

from dataclasses import replace
import re

from ecommerce.infrastructure.storage.platform_accounts import AccountStore
from ecommerce.platforms.qianniu.identity import read_shop_identity
from ecommerce.platforms.qianniu.login import LoginStatus, inspect_login, is_login_required
from ecommerce.shared.exceptions import NeedsAttention


def account_session_directory(workspace, identifier):
    if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
        raise ValueError('会话编号无效')
    root = workspace.resolve() / 'storage/managed_accounts/qianniu'
    directory = (root / identifier).resolve()
    if directory != root / identifier:
        raise ValueError('会话目录不在千牛账号资料下')
    return directory


def shop_choices(settings, recent=None):
    shops = {}
    for row in AccountStore(settings).list_accounts() if settings.task_backend == 'mysql' else ():
        shop = shops.setdefault(row['shop_id'], dict(id=row['shop_id'], name=row['shop_name'],
            platform_id=row['platform_shop_id'], accounts=[]))
        reason = account_problem(settings, row)
        shop['accounts'].append(dict(id=row['id'], name=row['login_name'], reason=reason,
            recent=not reason and (recent or {}).get(row['shop_id']) == row['id']))
    for shop in shops.values():
        shop['accounts'].sort(key=lambda item: (bool(item['reason']), not item['recent'],
                                                 item['name'].casefold(), item['id']))
    return sorted(shops.values(), key=lambda shop: (shop['name'], shop['platform_id']))


def account_problem(settings, account):
    if not account['enabled']:
        return '已停用'
    if account['login_required'] or not account['session_id']:
        return '需重新登录'
    try:
        account_settings(settings, account)
    except (NeedsAttention, ValueError):
        return '本机会话缺失，需重新登录'
    return ''


def selected_account(settings, account_id, shop_id):
    if any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value)
           for value in (account_id, shop_id)):
        raise NeedsAttention('请先选择店铺及该店可用的千牛账号')
    row = next((row for row in AccountStore(settings).list_accounts(account_id) if row['shop_id'] == shop_id), None)
    if row is None:
        raise NeedsAttention('所选账号与店铺不匹配，请重新选择')
    if problem := account_problem(settings, row):
        raise NeedsAttention(f'千牛账号 {row["login_name"]}（{problem}），请前往账号管理')
    return row


def account_target(row):
    return {'account_id': row['id'], **{key: row[key] for key in (
        'shop_id', 'login_name', 'shop_name', 'platform_shop_id',
    )}}


def account_settings(settings, row):
    directory = account_session_directory(settings.workspace, row['session_id'])
    session_path = directory / 'session.json'
    if not session_path.is_file():
        raise NeedsAttention(f'未找到 {row["login_name"]} 的本机会话，请前往账号管理重新登录')
    return replace(settings, profile_path=directory / 'browser', session_path=session_path)


def require_login(settings, row, message='千牛会话已失效，请到账号管理重新登录所选账号'):
    AccountStore(settings).change_state(row, 'require_login')
    raise NeedsAttention(message)


def check_identity(identity, target):
    if identity is None or not identity.login_name:
        raise NeedsAttention('无法核实当前千牛账号和店铺，任务已暂停')
    if identity.login_name != target['login_name']:
        raise NeedsAttention(f'千牛账号不符：预期 {target["login_name"]}，实际 {identity.login_name}')
    if identity.shop_id != target['platform_shop_id']:
        raise NeedsAttention(f'千牛店铺不符：预期店铺 ID {target["platform_shop_id"]}，实际 {identity.shop_id}')


def verify_session(page, target, on_expiry):
    status = inspect_login(page)
    if status is LoginStatus.REQUIRED:
        on_expiry()
    if status is not LoginStatus.AUTHENTICATED:
        raise NeedsAttention('暂时无法确认千牛会话，请检查网络或页面后重试')
    identity = read_shop_identity(page)
    if identity is None and is_login_required(page):
        on_expiry()
    check_identity(identity, target)
    return identity
