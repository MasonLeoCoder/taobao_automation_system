"""第十九版：通用分析来源支持问题及其回答快照。"""
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v18 import build_metadata as previous_metadata, NEW_TABLES
REVISION='0019_question_analysis'
PREVIOUS_REVISION='0018_report_checks'
COMMENTS={
 ('analysis_tasks','analysis_type'):'分析类型：reviews评价；questions问大家；各类型规则独立',
 ('analysis_tasks','source_count'):'输入来源数：reviews为评价条数；questions为问题数，回答随问题快照保存；不是人数或销量',
 ('analysis_sources','batch_id'):'来源采集批次：关联collection_batches.id，业务核对商品、模块及已发布状态',
 ('analysis_sources','review_id'):'原评价编号：与batch_id复合关联review_observations；问答分析为NULL；与question_id恰选一个',
 ('analysis_sources','snapshot'):'冻结输入：reviews沿用原契约；questions-source-v1为id/text/answers[{id,text,sku/date/identity_labels/purchase_labels可选}]/answers_complete/answer_stop_reason/date可选；不含昵称媒体密钥',
 ('analysis_sources','scope'):'采集范围快照：批次时间、覆盖及停止依据，非平台全量保证',
 ('analysis_reports','content'):'保留旧评价格式；questions-report-v1含markdown/generation/validation/phases/coverage/inspection/detail_revisions等，引用仅核对连续原文，不证明语义性能',
}
def build_metadata():
    """继承冻结结构并扩展互斥来源；参数：无；返回：模型。"""
    m=previous_metadata();s=m.tables['analysis_sources'];s.c.review_id.nullable=True
    s.comment='通用分析来源：评价或问题二选一，回答保留在问题快照内'
    s.append_column(sa.Column('question_id',m.tables['question_observations'].c.id.type.copy(),nullable=True,comment='原问题编号：与batch_id复合关联question_observations；评价分析为NULL；回答由问题快照保存'))
    s.append_constraint(sa.ForeignKeyConstraint(['question_id','batch_id'],['question_observations.id','question_observations.batch_id'],name='fk_analysis_source_question',ondelete='RESTRICT',onupdate='RESTRICT'))
    s.append_constraint(sa.CheckConstraint('(review_id IS NOT NULL AND question_id IS NULL) OR (review_id IS NULL AND question_id IS NOT NULL)',name='ck_analysis_source_kind'))
    s.append_constraint(sa.UniqueConstraint('analysis_id','question_id',name='uq_analysis_source_question'))
    for (t,c),comment in COMMENTS.items():m.tables[t].c[c].comment=comment
    return m
