"""记录账号启用状态与可重试的本机会话清理。"""

from alembic import op
import sqlalchemy as sa

from ecommerce.infrastructure.storage.mysql_migrations.schema_v30 import (
    ACCOUNT_COLUMNS, CLEANUP_TABLE, PREVIOUS_REVISION, REVISION, build_metadata,
)

revision = REVISION
down_revision = PREVIOUS_REVISION
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    tables = build_metadata().tables
    accounts = tables['platform_accounts']
    existing = {column['name']: column for column in sa.inspect(connection).get_columns(accounts.name)}
    for name in ACCOUNT_COLUMNS:
        if name not in existing:
            op.add_column(accounts.name, accounts.c[name]._copy())
    session = accounts.c.session_id
    if not existing['session_id']['nullable'] or existing['session_id']['comment'] != session.comment:
        op.alter_column(accounts.name, session.name, existing_type=session.type,
                        nullable=True, comment=session.comment)
    tables[CLEANUP_TABLE].create(connection, checkfirst=True)


def downgrade():
    raise RuntimeError('账号状态和会话清理待办必须保留')
