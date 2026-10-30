"""直接报告字段原生说明；不修改旧任务或数据结构。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v17 import build_metadata,COMMENT_UPDATES

revision='0017_direct_review_report'
down_revision='0016_review_pipeline'
branch_labels=None
depends_on=None


def upgrade():
    """更新八条原生备注；参数：迁移连接；返回：无。"""
    metadata=build_metadata()
    for table,column in COMMENT_UPDATES:
        item=metadata.tables[table].c[column]
        op.alter_column(table,column,existing_type=item.type,existing_nullable=item.nullable,comment=item.comment)


def downgrade():
    """禁止隐式回退含义；参数：无；返回：拒绝。"""
    raise RuntimeError('直接评价报告语义不允许隐式回退')
