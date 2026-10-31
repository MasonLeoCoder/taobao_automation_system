"""扩展来源关联，保留原评价行及旧迁移。"""
from alembic import op
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v19 import build_metadata,COMMENTS
revision='0019_question_analysis'
down_revision='0018_report_checks'
branch_labels=depends_on=None
def upgrade():
    """增加问题关联及互斥校验；参数：迁移连接；返回：无。"""
    m=build_metadata();s=m.tables['analysis_sources']
    # MySQL DDL不是事务；本轮升级未完成时允许核对空新增列后继续，不删除来源。
    inspector=sa.inspect(op.get_bind())
    existing={c['name']:c for c in inspector.get_columns(s.name)}
    if 'question_id' not in existing:op.add_column(s.name,s.c.question_id.copy())
    else:
        if op.get_bind().scalar(sa.text('SELECT COUNT(*) FROM analysis_sources WHERE question_id IS NOT NULL')):
            raise RuntimeError('未完成升级已有问题来源，拒绝修改')
        op.alter_column(s.name,'question_id',existing_type=existing['question_id']['type'],type_=s.c.question_id.type,existing_nullable=True,comment=s.c.question_id.comment)
    for (t,c) in COMMENTS:
        col=m.tables[t].c[c]
        op.alter_column(t,c,existing_type=col.type,nullable=col.nullable,comment=col.comment)
    op.create_table_comment(s.name,s.comment,existing_comment='分析来源表：保存明确评价关联及脱离后续采集变化的输入快照')
    op.create_foreign_key('fk_analysis_source_question',s.name,'question_observations',['question_id','batch_id'],['id','batch_id'],ondelete='RESTRICT',onupdate='RESTRICT')
    op.create_unique_constraint('uq_analysis_source_question',s.name,['analysis_id','question_id'])
    op.create_check_constraint('ck_analysis_source_kind',s.name,'(review_id IS NOT NULL AND question_id IS NULL) OR (review_id IS NULL AND question_id IS NOT NULL)')
def downgrade():
    """禁止丢失问题来源；参数：无；返回：拒绝。"""
    raise RuntimeError('问答来源不允许隐式删除')
