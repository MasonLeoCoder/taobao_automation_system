"""第二十版：综合选品分析复用五表，仅更新原生字段备注。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v19 import build_metadata as previous_metadata, NEW_TABLES

REVISION = '0020_composite_analysis'
PREVIOUS_REVISION = '0019_question_analysis'
COMMENT_UPDATES = {
    ('analysis_tasks', 'analysis_type'): '分析类型：reviews评价、questions问大家、composite基于两份固定报告的综合选品；规则各自独立',
    ('analysis_tasks', 'source_count'): '来源数：reviews评价条数、questions问题数；composite为所选报告实际引用并冻结的评价或问题行数，不是报告数、人数或销量',
    ('analysis_tasks', 'rules'): '创建时冻结规则；旧版原样保留；composite-v1含report/group及composite-input-v1来源格式、markdown输出，不含密钥',
    ('analysis_tasks', 'parameters'): '冻结计划预算与配置引用；composite-v1在plan.input保存两份基础报告id/analysis_id/version/content_hash/正文/范围/去重依据及商品背景；messages为实际发送快照；未自动重试扩额，无密钥',
    ('analysis_sources', 'snapshot'): '冻结脱敏原文；reviews及questions沿原契约；composite仅保存基础报告引用的评价/问题原快照，实际发送段另存plan.input.reports[].evidence；不含昵称媒体密钥',
    ('analysis_sources', 'scope'): '原采集范围快照：时间、覆盖、上限及停止依据；综合分析保留基础来源范围，不代表平台全量',
    ('analysis_reports', 'content'): '旧格式保留；composite-report-v1含markdown/generation/validation/phases/source_reports/source_hash/inspection/detail_revisions空映射；来源报告逻辑关联由参数固定，无新增外键；引用匹配不代表语义实测',
}


def build_metadata():
    """继承冻结表结构仅改备注；参数：无；返回：完整模型。"""
    metadata = previous_metadata()
    for (table, column), comment in COMMENT_UPDATES.items():
        metadata.tables[table].c[column].comment = comment
    return metadata
