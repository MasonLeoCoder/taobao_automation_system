"""第二十四版：四类交叉报告固定来源及定位检查，仅扩充原生备注。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v23 import build_metadata as previous_metadata, NEW_TABLES

REVISION = '0024_cross_analysis'
PREVIOUS_REVISION = '0023_image_kind_comments'
COMMENT_UPDATES = {
    ('analysis_tasks','analysis_type'): '分析类型：reviews评价、questions问大家、main_images主图、detail_images详情；cross为2至4类固定报告的交叉分析；composite和images为旧联合历史',
    ('analysis_tasks','rule_version'): '创建时固定规则版本；cross-v1四类自由组合，main-images-v1独立主图、detail-images-v1独立详情；其他历史规则保留',
    ('analysis_tasks','source_count'): '冻结来源行数：评价条数、问题数、图片原媒体数；cross为基础报告相关评价/问题/媒体去重行数，不是报告数、切片数、人数或销量',
    ('analysis_tasks','rules'): '创建时固定提示与规则；cross-v1含公共规则和组合重点report/group、章节及cross-input-v1格式；不随提示文件变化，不含密钥',
    ('analysis_tasks','parameters'): '冻结计划预算及配置引用；cross-v1在plan.input保存categories/combination/warnings及2至4份reports的编号、版本、哈希、正文、范围与相关依据；无自动重试扩额、无密钥',
    ('analysis_sources','snapshot'): '冻结原来源快照：评价或问题脱敏文字、图片媒体元数据；cross复用所选基础报告的相关来源，实际发送仅报告正文及evidence，不发送原图或机器路径',
    ('analysis_sources','scope'): '原采集范围：时间、覆盖、上限及停止依据；cross继承基础来源限制；同原媒体多个切片不算多份独立依据',
    ('analysis_sources','media_id'): '原媒体外键media_references.id；图片分析或cross图片报告来源填写，其余NULL；与review_id/question_id三选一，程序核对同商品发布批次及角色',
    ('analysis_reports','content'): '旧版本原样保留；cross-report-v1含markdown/generation/validation/phases/categories/combination/warnings/source_reports/source_hash/inspection/detail_revisions；来源报告逻辑关联无新外键；定位通过不证明语义或性能',
}


def build_metadata():
    """继承冻结结构，只更新九条业务备注，不增删业务行、列或约束。"""
    metadata = previous_metadata()
    for (table,column),comment in COMMENT_UPDATES.items(): metadata.tables[table].c[column].comment = comment
    return metadata
