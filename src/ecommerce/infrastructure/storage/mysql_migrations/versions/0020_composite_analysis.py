"""综合分析复用五表，只补充原生中文备注，不改业务行或约束。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v20 import build_metadata, COMMENT_UPDATES

revision = '0020_composite_analysis'
down_revision = '0019_question_analysis'
branch_labels = depends_on = None


def upgrade():
    """更新明确的字段备注；参数：迁移连接；返回：无。"""
    metadata = build_metadata()
    for table, column in COMMENT_UPDATES:
        value = metadata.tables[table].c[column]
        op.alter_column(table, column, existing_type=value.type, existing_nullable=value.nullable, comment=value.comment)


def downgrade():
    """拒绝隐式降级契约；参数：无；返回：拒绝。"""
    raise RuntimeError('综合分析契约不允许隐式降级')
