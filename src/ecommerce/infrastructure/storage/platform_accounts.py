"""登记已核验的平台账号及本机会话引用。"""

from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert

from .mysql import MySQLStorageError, safe_error
from .mysql_migrations.schema_v32 import build_metadata
from .time_values import database_now
from ecommerce.shared.exceptions import NeedsAttention


class AccountStore:
    def __init__(self, settings):
        self.engine = settings.task_database.engine
        tables = build_metadata().tables
        self.accounts = tables['platform_accounts']
        self.shops = tables['shops']
        self.links = tables['account_shops']
        self.cleanup = tables['account_session_cleanup']

    def list_accounts(self, account_id=None):
        accounts, shops, links = self.accounts, self.shops, self.links
        query = sa.select(accounts.c.id, accounts.c.login_name, accounts.c.session_id,
            accounts.c.enabled, accounts.c.login_required, accounts.c.revision, shops.c.id.label('shop_id'),
            accounts.c.verified_at, shops.c.platform_shop_id, shops.c.name.label('shop_name'),
            sa.exists().where(self.cleanup.c.account_id == accounts.c.id).label('cleanup_pending'),
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

    def list_buyers(self, account_id=None):
        """读取淘宝买家账号，不关联店铺。"""
        accounts = self.accounts
        query = sa.select(accounts, sa.exists().where(self.cleanup.c.account_id == accounts.c.id)
                          .label('cleanup_pending')).where(accounts.c.platform == 'taobao').order_by(accounts.c.login_name)
        if account_id is not None:
            query = query.where(accounts.c.id == account_id)
        try:
            with self.engine.connect() as connection:
                return [dict(row) for row in connection.execute(query).mappings()]
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None

    def record_buyer(self, nickname, session_id, *, current=None):
        """登记经过核验的买家昵称，或原子替换当前账号的会话。"""
        if current is not None and nickname != current['login_name']:
            raise NeedsAttention('淘宝昵称与登记的账号不符，保留原会话')
        try:
            with self.engine.begin() as connection:
                self._record_account(connection, 'taobao', nickname, session_id, database_now(), current)
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None

    def _record_account(self, connection, platform, login_name, session_id, now, current):
        accounts = self.accounts
        if current is None:
            duplicate = connection.scalar(sa.select(accounts.c.id).where(
                accounts.c.platform == platform, accounts.c.login_name == login_name))
            if duplicate:
                raise NeedsAttention('此账号已登记，请使用对应账号的重新登录')
            account_id = uuid4().hex
            connection.execute(accounts.insert().values(id=account_id, platform=platform,
                login_name=login_name, session_id=session_id, verified_at=now))
            return account_id
        expected = self._current(current) & accounts.c.enabled.is_(True) & (accounts.c.platform == platform)
        if session_id == current['session_id'] and platform == 'qianniu':
            expected &= accounts.c.login_required.is_(False)
        self._update(connection, expected, session_id=session_id, login_required=False, verified_at=now)
        if current['session_id'] and session_id != current['session_id']:
            self._queue_cleanup(connection, current)
        return current['id']

    def record(self, identity, session_id, *, current=None):
        """在一个事务登记身份，或替换已核验账号的会话引用。"""
        if current is not None and (identity.login_name, identity.shop_id) != (
            current['login_name'], current['platform_shop_id'],
        ):
            raise NeedsAttention('账号或店铺与已登记的身份不符')
        shops, links = self.shops, self.links
        now = database_now()
        try:
            with self.engine.begin() as connection:
                account_id = self._record_account(connection, 'qianniu', identity.login_name, session_id, now, current)

                shop = insert(shops).values(id=uuid4().hex, platform='taobao',
                    platform_shop_id=identity.shop_id, name=identity.shop_name)
                connection.execute(shop.on_duplicate_key_update(name=shop.inserted.name))
                shop_id = connection.scalar(sa.select(shops.c.id).where(
                    shops.c.platform == 'taobao', shops.c.platform_shop_id == identity.shop_id))
                link = insert(links).values(account_id=account_id, shop_id=shop_id, verified_at=now)
                connection.execute(link.on_duplicate_key_update(verified_at=link.inserted.verified_at))
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None

    def _current(self, current):
        accounts = self.accounts
        return sa.and_(accounts.c.id == current['id'], accounts.c.revision == current['revision'],
                       accounts.c.session_id == current['session_id'])

    def _update(self, connection, expected, **values):
        accounts = self.accounts
        updated = connection.execute(accounts.update().where(expected).values(
            revision=accounts.c.revision + 1, **values))
        if updated.rowcount != 1:
            raise NeedsAttention('账号状态已变化，请刷新列表后重试')

    def _queue_cleanup(self, connection, current):
        connection.execute(self.cleanup.insert().values(
            account_id=current['id'], session_id=current['session_id']))

    def change_state(self, current, action):
        """在删除文件前撤下会话并记录清理目标。"""
        state = {'disable': {'enabled': False}, 'enable': {'enabled': True},
                 'require_login': {'login_required': True},
                 'clear': {'session_id': None, 'login_required': True}}[action]
        try:
            with self.engine.begin() as connection:
                self._update(connection, self._current(current), **state)
                if action == 'clear' and current['session_id']:
                    self._queue_cleanup(connection, current)
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None

    def cleanup_sessions(self, account_id, remove):
        """处理该账号仍未完成且已撤下的目录，失败时保留待办。"""
        pending = self.cleanup
        active = sa.exists().where(self.accounts.c.session_id == pending.c.session_id)
        try:
            with self.engine.begin() as connection:
                identifiers = list(connection.scalars(sa.select(pending.c.session_id)
                    .where(pending.c.account_id == account_id, ~active).with_for_update()))
                for identifier in identifiers:
                    if remove(identifier):
                        connection.execute(pending.delete().where(
                            pending.c.account_id == account_id, pending.c.session_id == identifier))
                return bool(connection.scalar(sa.select(sa.exists().where(pending.c.account_id == account_id))))
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None
