"""绑定网页上架目标并加载指定千牛账号的有效会话。"""

from dataclasses import replace
import re

from ecommerce.infrastructure.storage.platform_accounts import AccountStore
from ecommerce.modules.account.web_accounts import account_session_directory
from ecommerce.shared.exceptions import NeedsAttention


def listing_accounts(settings):
    return AccountStore(settings).list_accounts() if settings.task_backend == 'mysql' else []


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
        raise NeedsAttention('所选账号与店铺的关联已变化，请重新预检')
    if problem := account_problem(settings, row):
        raise NeedsAttention(f'千牛账号 {row["login_name"]}（{problem}），请前往账号管理')
    return row


def listing_target(row):
    return {'account_id': row['id'], **{key: row[key] for key in (
        'shop_id', 'login_name', 'shop_name', 'platform_shop_id',
    )}}


def account_settings(settings, row):
    directory = account_session_directory(settings.workspace, row['session_id'])
    session_path = directory / 'session.json'
    if not session_path.is_file():
        raise NeedsAttention(f'未找到 {row["login_name"]} 的本机会话，请前往账号管理重新登录')
    return replace(settings, profile_path=directory / 'browser', session_path=session_path)
