"""报告生成与引用检查分离的原生字段说明，不修改业务行。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v18 import build_metadata,COMMENT_UPDATES

revision='0018_report_checks'
down_revision='0017_direct_review_report'
branch_labels=None
depends_on=None


def upgrade():
    """更新三条原生备注；参数：迁移连接；返回：无。"""
    metadata=build_metadata()
    for table,column in COMMENT_UPDATES:
        item=metadata.tables[table].c[column]
        op.alter_column(table,column,existing_type=item.type,existing_nullable=item.nullable,comment=item.comment)


def downgrade():
    """禁止隐式回退检查语义；参数：无；返回：拒绝。"""
    raise RuntimeError('报告本地检查语义不允许隐式回退')
