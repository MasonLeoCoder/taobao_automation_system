"""测试用千牛归属证据与独立浏览器路径。"""
from dataclasses import replace

from ecommerce.modules.account.managed import account_session_directory, account_target
from ecommerce.modules.product.service import open_publish
from ecommerce.platforms.qianniu.identity import ShopIdentity


ACCOUNT = dict(id='a' * 32, shop_id='b' * 32, session_id='c' * 32, enabled=True,
               login_required=False, login_name='tb123:测试', shop_name='测试店铺', platform_shop_id='123')
TARGET = account_target(ACCOUNT)
TARGET_FORM = {key: TARGET[key] for key in ('account_id', 'shop_id')}
IDENTITY = ShopIdentity('123', '测试店铺', '测试-运营', 'tb123:测试')


def fixture_session_file(settings):
    session = account_session_directory(settings.workspace, ACCOUNT['session_id']) / 'session.json'
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text('{}', encoding='utf-8')
    return session


def open_managed_publish(settings, *args, **kwargs):
    session = fixture_session_file(settings)
    isolated = replace(settings, task_backend='mysql', session_path=session, profile_path=session.parent / 'browser')

    def unexpected_expiry():
        raise AssertionError('假会话不应进入重新登录流程')

    return open_publish(isolated, *args, qianniu_target=TARGET, on_session_expired=unexpected_expiry, **kwargs)
