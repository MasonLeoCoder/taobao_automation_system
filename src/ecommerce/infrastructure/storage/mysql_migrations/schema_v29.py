"""第二十九版冻结模型，用于登记千牛账号和店铺。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from .schema_v28 import build_metadata as previous_metadata

REVISION = '0029_platform_accounts'
PREVIOUS_REVISION = '0028_sycm_search'
NEW_TABLES = ('platform_accounts', 'shops', 'account_shops')


def build_metadata():
    metadata = previous_metadata()
    common = dict(mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_bin')
    uuid = sa.String(32, collation='ascii_bin')
    code = sa.String(16, collation='ascii_bin')
    timestamp = mysql.DATETIME(fsp=6)
    sa.Table('platform_accounts', metadata,
        sa.Column('id', uuid, primary_key=True, comment='项目内部固定账号编号，32位小写UUID'),
        sa.Column('platform', code, nullable=False, comment='登录平台，本版为qianniu千牛'),
        sa.Column('login_name', sa.String(255), nullable=False, comment='平台完整登录名，子账号含店铺账号名与冒号'),
        sa.Column('session_id', uuid, nullable=False, comment='当前启用的本机会话目录编号，不含会话内容'),
        sa.Column('verified_at', timestamp, nullable=False, comment='最近一次确认账号及店铺的时间，北京时间'),
        sa.UniqueConstraint('platform', 'login_name', name='uq_platform_account_login'),
        sa.UniqueConstraint('session_id', name='uq_platform_account_session'),
        comment='平台登录账号登记，浏览器会话资料独立保存在所属环境的本机工作区', **common)
    sa.Table('shops', metadata,
        sa.Column('id', uuid, primary_key=True, comment='项目内部固定店铺编号，32位小写UUID'),
        sa.Column('platform', code, nullable=False, comment='店铺所属平台，本版为taobao淘宝'),
        sa.Column('platform_shop_id', sa.String(64, collation='ascii_bin'), nullable=False, comment='千牛店铺信息页显示的店铺ID'),
        sa.Column('name', sa.String(255), nullable=False, comment='最近一次核验时页面显示的店铺名称'),
        sa.UniqueConstraint('platform', 'platform_shop_id', name='uq_shop_platform_id'),
        comment='经平台页面核验的店铺登记', **common)
    sa.Table('account_shops', metadata,
        sa.Column('account_id', uuid, sa.ForeignKey('platform_accounts.id', name='fk_account_shop_account',
            onupdate='RESTRICT', ondelete='RESTRICT'), primary_key=True, comment='关联的平台账号编号'),
        sa.Column('shop_id', uuid, sa.ForeignKey('shops.id', name='fk_account_shop_shop',
            onupdate='RESTRICT', ondelete='RESTRICT'), primary_key=True, comment='关联的店铺编号'),
        sa.Column('verified_at', timestamp, nullable=False, comment='最近一次核实账号可以打开该店信息页的时间，北京时间'),
        comment='账号与店铺的核验关系，允许同店多个账号及一个账号关联多个店铺', **common)
    return metadata
