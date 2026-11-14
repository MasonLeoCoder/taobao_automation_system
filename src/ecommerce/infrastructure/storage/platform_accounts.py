"""登记已核验的千牛账号、店铺及本机会话引用。"""

from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert

from .mysql import MySQLStorageError, safe_error
from .mysql_migrations.schema_v29 import build_metadata
from .time_values import database_now
from ecommerce.shared.exceptions import NeedsAttention


class AccountStore:
    def __init__(self, settings):
        self.engine = settings.task_database.engine
        tables = build_metadata().tables
        self.accounts = tables['platform_accounts']
        self.shops = tables['shops']
        self.links = tables['account_shops']

    def list_accounts(self, account_id=None):
        accounts, shops, links = self.accounts, self.shops, self.links
        query = sa.select(accounts.c.id, accounts.c.login_name, accounts.c.session_id,
            accounts.c.verified_at, shops.c.platform_shop_id, shops.c.name.label('shop_name'),
        ).select_from(accounts).join(links, links.c.account_id == accounts.c.id).join(shops, shops.c.id == links.c.shop_id).where(
            accounts.c.platform == 'qianniu', shops.c.platform == 'taobao',
        ).order_by(accounts.c.login_name, shops.c.name)
        if account_id is not None:
            query = query.where(accounts.c.id == account_id)
        try:
            with self.engine.connect() as connection:
                return [dict(row) for row in connection.execute(query).mappings()]
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None

    def record(self, identity, session_id, *, current=None):
        """在一个事务登记身份，或替换已核验账号的会话引用。"""
        if current is not None and (identity.login_name, identity.shop_id) != (
            current['login_name'], current['platform_shop_id'],
        ):
            raise NeedsAttention('账号或店铺与已登记的身份不符')
        accounts, shops, links = self.accounts, self.shops, self.links
        now = database_now()
        try:
            with self.engine.begin() as connection:
                if current is None:
                    account_id = uuid4().hex
                    duplicate = connection.scalar(sa.select(accounts.c.id).where(
                        accounts.c.platform == 'qianniu', accounts.c.login_name == identity.login_name))
                    if duplicate:
                        raise NeedsAttention('此账号已登记，请使用对应账号的重新登录')
                    connection.execute(accounts.insert().values(id=account_id, platform='qianniu',
                        login_name=identity.login_name, session_id=session_id, verified_at=now))
                else:
                    account_id = current['id']
                    updated = connection.execute(accounts.update().where(
                        accounts.c.id == account_id, accounts.c.session_id == current['session_id'],
                        accounts.c.login_name == identity.login_name,
                    ).values(session_id=session_id, verified_at=now))
                    if updated.rowcount != 1:
                        raise NeedsAttention('账号会话记录已变化，请刷新后重试')

                shop = insert(shops).values(id=uuid4().hex, platform='taobao',
                    platform_shop_id=identity.shop_id, name=identity.shop_name)
                connection.execute(shop.on_duplicate_key_update(name=shop.inserted.name))
                shop_id = connection.scalar(sa.select(shops.c.id).where(
                    shops.c.platform == 'taobao', shops.c.platform_shop_id == identity.shop_id))
                link = insert(links).values(account_id=account_id, shop_id=shop_id, verified_at=now)
                connection.execute(link.on_duplicate_key_update(verified_at=link.inserted.verified_at))
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None
