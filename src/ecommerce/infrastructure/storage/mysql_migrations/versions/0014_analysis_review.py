"""追加报告复核表及新版契约备注，不改写业务数据或历史迁移。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v14 import build_metadata,COMMENT_UPDATES

revision='0014_analysis_review'
down_revision='0013_analysis_recovery'
branch_labels=None
depends_on=None


def upgrade():
    """升级约束、备注及复核表；参数：当前迁移连接；返回：无。"""
    m=build_metadata()
    op.drop_constraint('ck_analysis_call_kind','analysis_calls',type_='check')
    op.create_check_constraint('ck_analysis_call_kind','analysis_calls',"(kind = 'classify' AND chunk_index > 0) OR (kind = 'report' AND chunk_index >= 0)")
    op.create_unique_constraint('uq_analysis_report_owner','analysis_reports',['id','analysis_id'])
    m.tables['analysis_review_decisions'].create(op.get_bind())
    for name,column in COMMENT_UPDATES:
        field=m.tables[name].c[column]
        op.alter_column(name,column,existing_type=field.type,existing_nullable=field.nullable,comment=field.comment)


def downgrade():
    """保护已有人工决定和报告分段；参数：无；返回：拒绝隐式删除。"""
    raise RuntimeError('复核记录和分段报告须单独核对，不允许隐式回退')
