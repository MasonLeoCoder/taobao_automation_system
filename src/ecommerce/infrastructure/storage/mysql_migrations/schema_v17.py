"""第十七版冻结模型：直接报告的JSON和计数口径，仅原生备注变更。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v16 import build_metadata as previous_metadata,NEW_TABLES

REVISION='0017_direct_review_report'
PREVIOUS_REVISION='0016_review_pipeline'
COMMENT_UPDATES={
 ('analysis_tasks','rules'):'创建时冻结规则；reviews-v6/schema_version=6含主报告report、分组归纳group、source_format与output_format=markdown；旧规则原样保留，无密钥',
 ('analysis_tasks','parameters'):'冻结计划及预算；新plan.version=6含模型能力/本地估算工具身份/真实消息分组/汇总上界/输出上限/估算USD，budget_version=3沿用确认实际量释放预留，未知仍占额度；补处理另存，不自动重试',
 ('analysis_tasks','fingerprint'):'规范JSON SHA256；旧任务沿原算法；reviews-v6按商品/批次/完整来源/标题/规则/服务商/模型/报告输出上限生成成品身份，不含价格、容量、分词器、预算和超时；发送计划哈希另在parameters',
 ('analysis_tasks','processed_count'):'旧版已保存逐条分类/提取数量；reviews-v6不做逐条分类，此列保持0，页面使用报告coverage来源数/提交尝试数/有效引用来源数及报告状态，不伪造逐条通过数',
 ('analysis_calls','kind'):'classify为旧逐条提取；report为报告请求，chunk_index=0总报告或直接报告，正数为分组报告/简短归纳；v6只使用report',
 ('analysis_calls','request'):'固定请求；v6 messages为实际system/user消息，prompt仅供显示，measurement为预计输入/输出/余量及工具身份，request_hash绑定messages；source_ids保留来源归属，汇总消息待组返回构造并再校验；无认证信息',
 ('analysis_calls','usage'):'用量Token与本地reserved_tokens；unknown=true占用保守预留，false且total_tokens明确则budget_version>=2用实际量；v6预留用模型分词估算或UTF8保守值加输出及余量，不等于人民币或实际扣款',
 ('analysis_reports','content'):'新reviews-report-v6保存markdown/validation/phases/issues/origin/coverage/basic_statistics/source_hash/detail_revisions空映射；quality完整/待复核/中断不代表语义已实测；来源/尝试提交/有效引用分开计数；旧报告不转换',
}


def build_metadata():
    """继承已执行结构只改备注；参数：无；返回：完整元数据。"""
    metadata=previous_metadata()
    for (table,column),comment in COMMENT_UPDATES.items():metadata.tables[table].c[column].comment=comment
    return metadata
