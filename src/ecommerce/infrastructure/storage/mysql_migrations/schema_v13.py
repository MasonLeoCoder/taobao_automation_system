"""第十三版冻结模型：逐条校验、局部恢复与补处理的原生备注。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v12 import build_metadata as previous_metadata, NEW_TABLES

REVISION = '0013_analysis_recovery'
PREVIOUS_REVISION = '0012_analysis_sending'
COMMENT_UPDATES = {
    ('analysis_tasks','rule_version'): '创建时固定的评价提示规则版本；旧快照保留，本地校验策略另记审计版本',
    ('analysis_tasks','parameters'): '任务参数：原发送与补处理计划、预算、逐条校验及规范化审计、配置文件引用；无密钥',
    ('analysis_calls','status'): '调用状态：inflight进行中、succeeded全部校验通过、invalid存在不合格项但可保留合格条目、unknown结果未知',
    ('analysis_calls','request'): '实际请求快照：提示、评价编号、输出及JSON和等待限制、可选补处理清单引用；无HTTP头和密钥',
    ('analysis_details','result'): '逐条校验合格分类：schema_version=1、kind及原文观点；可含有审计的本地规范化，不等于人工复核',
}


def build_metadata():
    """继承冻结结构并更新指定备注；参数：无；返回：表列约束不变的元数据。"""
    metadata=previous_metadata()
    for (table,column),comment in COMMENT_UPDATES.items():
        metadata.tables[table].c[column].comment=comment
    return metadata
