"""补齐新版JSON与复核原生备注；不改写已执行0014或业务内容。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v15 import build_metadata,COMMENT_UPDATES

revision='0015_analysis_comments'
down_revision='0014_analysis_review'
branch_labels=None
depends_on=None


def upgrade():
    """仅修改原生中文备注；参数：迁移连接；返回：无。"""
    m=build_metadata()
    for name,column in COMMENT_UPDATES:
        c=m.tables[name].c[column]
        op.alter_column(name,column,existing_type=c.type,existing_nullable=c.nullable,comment=c.comment)


def downgrade():
    """保护版本语义，不隐式回退；参数：无；返回：拒绝。"""
    raise RuntimeError('新版分析字段语义不允许隐式回退')
