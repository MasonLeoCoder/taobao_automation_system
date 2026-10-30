"""补齐新评价流水线JSON备注，不更改历史业务记录及结构。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v16 import build_metadata,COMMENT_UPDATES

revision='0016_review_pipeline'
down_revision='0015_analysis_comments'
branch_labels=None
depends_on=None


def upgrade():
    """更新五条原生备注；参数：迁移连接；返回：无。"""
    metadata=build_metadata()
    for name,column in COMMENT_UPDATES:
        item=metadata.tables[name].c[column]
        op.alter_column(name,column,existing_type=item.type,existing_nullable=item.nullable,comment=item.comment)


def downgrade():
    """不隐式回退JSON含义；参数：无；返回：拒绝。"""
    raise RuntimeError('新任务评价流水线语义不允许隐式回退')
