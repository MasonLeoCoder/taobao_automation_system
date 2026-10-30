"""第十六版模型：只扩充新任务JSON状态、紧凑发送及分段报告原生说明。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v15 import build_metadata as previous_metadata,NEW_TABLES

REVISION='0016_review_pipeline'
PREVIOUS_REVISION='0015_analysis_comments'
COMMENT_UPDATES={
 ('analysis_tasks','rules'):'创建时冻结规则；schema_version=4沿V1.2契约，5增加wire_format及allow_exclusions；提取/报告提示、输出契约、策略均快照，无密钥；旧任务不自动升级',
 ('analysis_tasks','parameters'):'冻结发送计划/预算/追加历史/validation_history；reviews-v5审计增加outcomes逐来源valid/partial/no_information/excluded及technical_failure，排除不自动重发；unfinished仅报告派生；安全同源归并映射在adjustments，原返回不改',
 ('analysis_calls','request'):'冻结实际prompt、source_ids、输出上限及JSON/等待参数；report_input为服务端完整校验视图，reviews-v5实际prompt用review-report-wire-v1列式无损表达；detail_revisions绑定明细；不保存密钥',
 ('analysis_details','result'):'原始合格提取；reviews-detail-v4/v5含观点及逐字依据；v5允许同源各片段独立校验后仅重编引用编号，映射见任务审计；局部无效观点隔离，空白或笼统属于已处理无具体信息',
 ('analysis_reports','content'):'不可覆盖报告快照；reviews-report-v5增加source_outcomes、coverage.state_counts和phase_reports；统计/提取/阶段稿/总稿/诊断分存，分段复核键segment:N:加原键；排除不等于无价值，执行故障不自动排除；旧报告不改',
}


def build_metadata():
    """仅继承结构并更新备注；参数：无；返回：32业务表元数据。"""
    metadata=previous_metadata()
    for (name,column),comment in COMMENT_UPDATES.items():metadata.tables[name].c[column].comment=comment
    return metadata
