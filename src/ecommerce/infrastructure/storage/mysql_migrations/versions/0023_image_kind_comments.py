"""补齐独立图片类型的三条字段备注，不修改业务值。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v23 import build_metadata, COMMENT_UPDATES

revision='0023_image_kind_comments'
down_revision='0022_separate_image_analysis'
branch_labels=depends_on=None


def upgrade():
    """按当前模型补齐规则、数量和来源说明。"""
    metadata=build_metadata()
    for table,column in COMMENT_UPDATES:
        value=metadata.tables[table].c[column]
        op.alter_column(table,column,existing_type=value.type,existing_nullable=value.nullable,comment=value.comment)


def downgrade():
    """拒绝隐式降级已明确的字段口径。"""
    raise RuntimeError('独立图片字段备注不允许隐式降级')
