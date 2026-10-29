"""仅更新分析局部恢复备注，不增删表列，不重写业务记录。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v13 import build_metadata,COMMENT_UPDATES

revision='0013_analysis_recovery'
down_revision='0012_analysis_sending'
branch_labels=None
depends_on=None


def upgrade():
    """同步逐条校验和补处理字段备注；参数：迁移连接；返回：无。"""
    metadata=build_metadata()
    for table,column in COMMENT_UPDATES:
        field=metadata.tables[table].c[column]
        op.alter_column(table,column,existing_type=field.type,existing_nullable=field.nullable,comment=field.comment)


def downgrade():
    """避免隐式回退已记录恢复语义；参数：无；返回：拒绝回退。"""
    raise RuntimeError('逐条恢复审计与补处理历史须单独核对，不允许隐式回退')
