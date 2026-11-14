"""第三十版冻结模型，记录千牛账号启停及本机会话清理。"""

import sqlalchemy as sa

from .schema_v29 import build_metadata as previous_metadata

REVISION = '0030_account_lifecycle'
PREVIOUS_REVISION = '0029_platform_accounts'
ACCOUNT_COLUMNS = ('enabled', 'login_required', 'revision')
CLEANUP_TABLE = 'account_session_cleanup'


def build_metadata():
    metadata = previous_metadata()
    accounts = metadata.tables['platform_accounts']
    accounts.c.session_id.nullable = True
    accounts.c.session_id.comment = '当前启用的本机会话目录编号，清除本机登录后为空'
    accounts.append_column(sa.Column('enabled', sa.Boolean, nullable=False,
        server_default=sa.text('1'), comment='账号是否启用，停用时禁止新建登录及打开会话操作'))
    accounts.append_column(sa.Column('login_required', sa.Boolean, nullable=False,
        server_default=sa.text('0'), comment='明确要求重新登录，平台已提示登录或本机会话已撤下'))
    accounts.append_column(sa.Column('revision', sa.BigInteger, nullable=False,
        server_default=sa.text('0'), comment='账号状态版本，避免从旧列表重复或误操作'))
    sa.Table(CLEANUP_TABLE, metadata,
        sa.Column('session_id', sa.String(32, collation='ascii_bin'), primary_key=True,
                  comment='已撤下的本机会话目录编号，目录删除完成后移除该记录'),
        sa.Column('account_id', sa.String(32, collation='ascii_bin'),
                  sa.ForeignKey('platform_accounts.id', name='fk_account_cleanup_account',
                                onupdate='RESTRICT', ondelete='RESTRICT'),
                  nullable=False, comment='待清理会话所属的平台账号'),
        sa.Index('ix_account_cleanup_account', 'account_id'),
        comment='本机会话清理待办，目录被占用或重启后可恢复',
        mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_bin')
    return metadata
