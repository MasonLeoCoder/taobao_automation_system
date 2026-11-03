"""主图与详情独立分析仅更新中文原生备注，不改旧业务行。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v22 import build_metadata, COMMENT_UPDATES

revision='0022_separate_image_analysis'
down_revision='0021_image_analysis'
branch_labels=depends_on=None


def upgrade():
    """更新独立分析的字段契约备注。"""
    metadata=build_metadata()
    for table,column in COMMENT_UPDATES:
        value=metadata.tables[table].c[column]
        op.alter_column(table,column,existing_type=value.type,existing_nullable=value.nullable,comment=value.comment)


def downgrade():
    """拒绝隐式降级独立报告契约。"""
    raise RuntimeError('独立图片分析契约不允许隐式降级')
