"""记录网页上架选定的账号和店铺。"""

from alembic import op
import sqlalchemy as sa

from ecommerce.infrastructure.storage.mysql_migrations.schema_v31 import (
    OWNERS, PREVIOUS_REVISION, REVISION, build_metadata,
)

revision = REVISION
down_revision = PREVIOUS_REVISION
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    table = build_metadata().tables['tasks']
    columns = {item['name'] for item in inspector.get_columns(table.name)}
    foreign_keys = {item['name'] for item in inspector.get_foreign_keys(table.name)}
    indexes = {item['name'] for item in inspector.get_indexes(table.name)}
    for field, (target, _) in OWNERS.items():
        if field not in columns:
            op.add_column(table.name, table.c[field]._copy())
        if 'ix_tasks_' + field not in indexes:
            op.create_index('ix_tasks_' + field, table.name, [field])
        if 'fk_tasks_' + field not in foreign_keys:
            op.create_foreign_key('fk_tasks_' + field, table.name, target, [field], ['id'],
                                  onupdate='RESTRICT', ondelete='RESTRICT')


def downgrade():
    raise RuntimeError('上架任务的账号及店铺归属必须保留')
