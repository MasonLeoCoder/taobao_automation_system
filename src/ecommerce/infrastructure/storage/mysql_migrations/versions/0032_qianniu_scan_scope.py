"""关联发布类目扫描的任务并更新千牛归属备注。"""

from alembic import op
import sqlalchemy as sa

from ecommerce.infrastructure.storage.mysql_migrations.schema_v32 import (
    PREVIOUS_REVISION, REVISION, SCOPE_COMMENTS, SCAN_TASK_COMMENT,
)

revision = REVISION
down_revision = PREVIOUS_REVISION
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    table = 'publish_category_scans'
    identity_type = sa.String(32, collation='ascii_bin')
    if not any(column['name'] == 'task_id' for column in inspector.get_columns(table)):
        op.add_column(table, sa.Column('task_id', identity_type, comment=SCAN_TASK_COMMENT))
    if not any(key['name'] == 'uq_publish_scan_task' for key in inspector.get_unique_constraints(table)):
        op.create_unique_constraint('uq_publish_scan_task', table, ['task_id'])
    if not any(key['name'] == 'fk_publish_scan_task' for key in inspector.get_foreign_keys(table)):
        op.create_foreign_key('fk_publish_scan_task', table, 'tasks', ['task_id'], ['id'],
                              onupdate='RESTRICT', ondelete='RESTRICT')
    previous = {col['name']: col['comment'] for col in inspector.get_columns('tasks')}
    for field, comment in SCOPE_COMMENTS.items():
        if previous[field] != comment:
            op.alter_column('tasks', field, existing_type=identity_type, existing_nullable=True,
                            existing_comment=previous[field], comment=comment)


def downgrade():
    raise RuntimeError('必须保留类目扫描的任务归属')
