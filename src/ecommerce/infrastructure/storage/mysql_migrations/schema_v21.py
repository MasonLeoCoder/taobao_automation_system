"""第二十一版：通用分析来源支持固定图片引用。"""
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v20 import build_metadata as previous_metadata, NEW_TABLES

REVISION = '0021_image_analysis'
PREVIOUS_REVISION = '0020_composite_analysis'
SOURCE_COMMENT = '通用分析来源：评价、问题或图片三选一；图片关联原媒体，切片及排除范围保留冻结快照'
COMMENT_UPDATES = {
    ('analysis_tasks', 'analysis_type'): '分析类型：reviews评价、questions问大家、composite固定报告综合、images主图与详情看图；规则独立',
    ('analysis_tasks', 'source_count'): '冻结来源行数：评价条数、问题数、综合实际引用行数、images纳入原图数；不是切片数、人数或销量',
    ('analysis_tasks', 'rules'): '冻结提示词及版本；images-v1含report/group、vision-tiles-v1处理规则；不含密钥',
    ('analysis_tasks', 'parameters'): '冻结计划、预算及配置引用；images-v1保存选定主图详情批次、原件索引哈希、切片坐标及摘要、排除原因、实际消息描述；base64仅发送时生成不入库；不自动重试扩额',
    ('analysis_sources', 'snapshot'): '冻结来源；评价问题沿原契约；images含原media_id/asset/角色/原序号/原件摘要及尺寸/切片坐标和摘要，不保存base64；原件不改写',
    ('analysis_sources', 'scope'): '固定采集范围与时间；images含来源模块批次及processing版本；不代表平台全量',
    ('analysis_reports', 'content'): '旧契约保留；images-report-v1含markdown/generation/validation/phases/source_images/source_hash/inspection/detail_revisions空映射；图片引用只核对本次发送定位，不证明OCR、语义或实测',
}


def build_metadata():
    """继承冻结结构，新增图片来源外键及三选一约束；返回：完整模型。"""
    metadata = previous_metadata(); source = metadata.tables['analysis_sources']
    source.comment = SOURCE_COMMENT
    source.append_column(sa.Column('media_id', metadata.tables['media_references'].c.id.type.copy(), nullable=True,
        comment='原媒体编号：关联media_references.id；仅images填写，须由程序核对同商品同发布批次及main_image/detail_image角色；其他类型NULL'))
    for constraint in list(source.constraints):
        if constraint.name == 'ck_analysis_source_kind': source.constraints.remove(constraint)
    source.append_constraint(sa.CheckConstraint('(review_id IS NOT NULL AND question_id IS NULL AND media_id IS NULL) OR '
        '(review_id IS NULL AND question_id IS NOT NULL AND media_id IS NULL) OR '
        '(review_id IS NULL AND question_id IS NULL AND media_id IS NOT NULL)', name='ck_analysis_source_kind'))
    source.append_constraint(sa.ForeignKeyConstraint(['media_id'], ['media_references.id'], name='fk_analysis_source_media', ondelete='RESTRICT', onupdate='RESTRICT'))
    source.append_constraint(sa.UniqueConstraint('analysis_id', 'media_id', name='uq_analysis_source_media'))
    for (table, column), comment in COMMENT_UPDATES.items(): metadata.tables[table].c[column].comment = comment
    return metadata
