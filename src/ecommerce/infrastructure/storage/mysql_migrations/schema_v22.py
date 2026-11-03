"""第二十二版：主图与详情独立分析的原生备注；不变更列和约束。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v21 import build_metadata as previous_metadata, NEW_TABLES

REVISION='0022_separate_image_analysis'
PREVIOUS_REVISION='0021_image_analysis'
COMMENT_UPDATES={
    ('analysis_tasks','analysis_type'): '分析类型：reviews评价、questions问大家、composite反馈综合、images旧联合图片；main_images独立主图、detail_images独立详情图，历史按类型隔离',
    ('analysis_tasks','rules'): '冻结提示及版本；main-images-v1六章、detail-images-v1七章，含module/sections/report_version及vision-tiles-v1；旧任务规则保留，不含密钥',
    ('analysis_tasks','parameters'): '冻结计划预算与配置引用；独立图片plan.input=images-input-v2，固定单模块单发布批次、原件索引/切片坐标摘要及排除原因；messages为可重建描述，base64仅内存发送；不自动重试扩额',
    ('analysis_sources','snapshot'): '冻结来源；独立主图/详情仅保存本模块原媒体及asset/尺寸/切片坐标摘要；旧联合及文字快照保留；不保存base64，不改写原件',
    ('analysis_sources','scope'): '原采集范围快照；独立图片含module/processing/batch_id，须与任务类型和固定批次一致；不代表平台实时全量',
    ('analysis_reports','content'): '原版本保留；main-images-report-v1六章、detail-images-report-v1七章并含module；markdown/generation/validation/source_images/scope/phases/inspection，图号只定位不验证OCR宣称或性能；本地检查追加版本',
}


def build_metadata():
    """继承冻结模型，仅更新本轮契约备注；返回：完整模型。"""
    metadata=previous_metadata()
    for (table,column),comment in COMMENT_UPDATES.items(): metadata.tables[table].c[column].comment=comment
    return metadata
