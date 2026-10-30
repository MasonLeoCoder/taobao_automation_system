"""第十五版冻结模型：补齐新版规则快照与复核边界的原生中文备注。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v14 import build_metadata as previous_metadata,NEW_TABLES

REVISION='0015_analysis_comments'
PREVIOUS_REVISION='0014_analysis_review'
COMMENT_UPDATES={
 ('analysis_tasks','rules'):'创建时冻结规则快照：旧schema_version=1提示/主题映射；新schema_version=4含V1.2提取/报告提示及完整输出契约和校验策略，无密钥',
 ('analysis_calls','kind'):'AI调用用途：classify为评价事实提取（旧任务为分类）；report由chunk_index区分分段报告和总报告',
 ('analysis_details','review_status'):'逐条修订状态：unreviewed未修订、corrected已人工修正；独立于报告逐项pending/adopted/rejected，不代表整份报告已审核',
 ('analysis_reports','content'):'不可覆盖版本快照：旧schema_version=1/2或新reviews-report-v4；新含提取快照/程序统计/覆盖/AI稿/诊断/待复核候选及来源身份；人工决定另表追加，读取时派生当前复核状态',
}


def build_metadata():
    """继承已执行0014结构，补充备注；参数：无；返回：完整元数据。"""
    m=previous_metadata()
    for (name,column),comment in COMMENT_UPDATES.items():m.tables[name].c[column].comment=comment
    return m
