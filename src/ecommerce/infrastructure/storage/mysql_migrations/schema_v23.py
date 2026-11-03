"""第二十三版：补齐独立图片的数量、规则版本及媒体来源原生备注。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v22 import build_metadata as previous_metadata, NEW_TABLES

REVISION='0023_image_kind_comments'
PREVIOUS_REVISION='0022_separate_image_analysis'
COMMENT_UPDATES={
    ('analysis_tasks','source_count'): '冻结来源行数：评价条数、问题数、综合引用行数；images旧联合和main_images/detail_images独立分析均为纳入原媒体数，不是去重切片数、人数或销量',
    ('analysis_tasks','rule_version'): '创建时固定分析规则版本；main-images-v1独立主图、detail-images-v1独立详情；reviews/questions/composite及images-v1旧规则按原版本保留',
    ('analysis_sources','media_id'): '原媒体编号：外键media_references.id；images旧联合及main_images/detail_images独立分析填写，其他类型NULL；程序核对同商品同发布批次及图片角色',
}


def build_metadata():
    """继承已执行冻结模型，仅补中文备注。"""
    metadata=previous_metadata()
    for (table,column),comment in COMMENT_UPDATES.items(): metadata.tables[table].c[column].comment=comment
    return metadata
