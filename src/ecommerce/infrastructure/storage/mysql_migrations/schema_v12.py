"""第十二版冻结模型：仅更新评价分析版本化参数及预算的原生备注。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v11 import build_metadata as previous_metadata, NEW_TABLES

REVISION = '0012_analysis_sending'
PREVIOUS_REVISION = '0011_analysis'
COMMENT_UPDATES = {
    ('analysis_tasks', 'rule_version'): '分析规则版本：reviews-v1兼容旧任务，reviews-v2按长度合并发送；随任务固定',
    ('analysis_tasks', 'rules'): '规则快照：版本化分类和报告提示、主题映射、统计口径及报告输入限制；不含密钥',
    ('analysis_tasks', 'parameters'): '任务参数快照：发送计划、预算版本与追加记录、配置引用；旧版保留批大小；不含密钥',
    ('analysis_calls', 'request'): '请求快照：实际提示文字、评价编号、输出上限及新版JSON模式与等待上限；无HTTP头或密钥',
    ('analysis_calls', 'usage'): '调用用量与保守预留：用量未确认时unknown=true；新版确认后按实际结算，旧版保留较大值',
    ('analysis_reports', 'content'): '版本化报告：统计、发现、建议、问题、明细修订及覆盖；新版区分逐条完成和综合报告完成',
}


def build_metadata():
    """继承冻结结构并更新备注；参数：无；返回：字段及约束不变的元数据。"""
    metadata = previous_metadata()
    for (table, column), comment in COMMENT_UPDATES.items():
        metadata.tables[table].c[column].comment = comment
    return metadata
