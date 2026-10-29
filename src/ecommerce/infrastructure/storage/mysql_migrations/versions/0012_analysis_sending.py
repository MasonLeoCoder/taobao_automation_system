"""更新分析参数与预算备注；不新增表、字段或改写业务数据。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v12 import build_metadata, COMMENT_UPDATES

revision = '0012_analysis_sending'
down_revision = '0011_analysis'
branch_labels = None
depends_on = None


def upgrade():
    """逐字段同步原生中文备注；参数：迁移连接；返回：无。"""
    metadata = build_metadata()
    for table, column in COMMENT_UPDATES:
        field = metadata.tables[table].c[column]
        op.alter_column(table, column, existing_type=field.type, existing_nullable=field.nullable, comment=field.comment)


def downgrade():
    """保留新版本参数含义；参数：无；返回：拒绝隐式回退。"""
    raise RuntimeError('已有分析版本和额度追加记录不可隐式回退，请先单独核对历史任务')
