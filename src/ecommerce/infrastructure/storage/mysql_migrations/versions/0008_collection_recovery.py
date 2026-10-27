"""新增采集恢复和补采表，不改写已有业务结果。"""
from alembic import op
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v8 import build_metadata

revision = '0008_collection_recovery'
down_revision = '0007_basic_info'
branch_labels = None
depends_on = None


def upgrade():
    """在无活动任务时幂等新增四表；参数：无；返回：无。"""
    connection = op.get_bind()
    if connection.scalar(sa.text('SELECT COUNT(*) FROM tasks WHERE finished_at IS NULL')):
        raise RuntimeError('存在未结束任务，暂不升级采集恢复结构')
    metadata = build_metadata()
    for name in ('collection_plans', 'collection_plan_executions', 'collection_discarded_batches', 'collection_retry_queue'):
        metadata.tables[name].create(connection, checkfirst=True)


def downgrade():
    """拒绝自动丢弃恢复和清理依据；参数：无；返回：异常。"""
    raise RuntimeError('恢复计划及清理凭据不可自动删除，回退须单独审核')
