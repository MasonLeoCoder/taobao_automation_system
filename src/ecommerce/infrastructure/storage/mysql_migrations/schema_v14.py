"""第十四版冻结模型：新版报告契约、分段报告调用及逐项复核审计。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from ecommerce.infrastructure.storage.mysql_migrations.schema_v13 import build_metadata as previous_metadata

REVISION='0014_analysis_review'
PREVIOUS_REVISION='0013_analysis_recovery'
NEW_TABLES=('analysis_review_decisions',)
COMMENT_UPDATES={
 ('analysis_tasks','rule_version'):'创建时固定的分析规则版本：旧reviews-v1/v2/v3保留，新任务reviews-v4采用事实提取及待复核选品报告',
 ('analysis_tasks','parameters'):'任务参数：固定发送计划、报告分段预留、预算与补处理及本地校验审计；仅配置文件引用，无密钥',
 ('analysis_calls','chunk_index'):'逻辑分组：classify从1开始；report为0表示总报告，正数表示分段报告；旧任务报告仍为0',
 ('analysis_calls','request'):'实际请求快照：提示、来源编号、输出/JSON/等待限制及可选report_input；无HTTP头和密钥',
 ('analysis_details','result'):'已校验逐条结果：旧schema_version=1分类或新reviews-detail-v4事实提取；原文观点、字段疑点及派生状态不等于人工确认',
 ('analysis_reports','content'):'版本报告：旧schema_version=1/2或新reviews-report-v4；来源/明细版本、覆盖、程序倾向统计、AI发现及待复核条目；历史不覆盖',
}


def build_metadata():
    """构建冻结版本；参数：无；返回：完整表结构。"""
    m=previous_metadata()
    for (name,column),comment in COMMENT_UPDATES.items():m.tables[name].c[column].comment=comment
    calls=m.tables['analysis_calls']
    for constraint in list(calls.constraints):
        if constraint.name=='ck_analysis_call_kind':calls.constraints.remove(constraint)
    calls.append_constraint(sa.CheckConstraint("(kind = 'classify' AND chunk_index > 0) OR (kind = 'report' AND chunk_index >= 0)",name='ck_analysis_call_kind'))
    reports=m.tables['analysis_reports']
    reports.append_constraint(sa.UniqueConstraint('id','analysis_id',name='uq_analysis_report_owner'))
    code=lambda n=32:sa.String(n,collation='ascii_bin')
    def col(name,kind,comment):
        """声明非空字段；参数：标识、类型、备注；返回：列。"""
        return sa.Column(name,kind,nullable=False,comment=comment)
    t=sa.Table('analysis_review_decisions',m,
        col('id',code(),'复核记录编号：32位小写UUID'),
        col('analysis_id',code(),'所属通用分析任务：关联analysis_tasks.id'),
        col('report_id',code(),'被审核的报告版本记录：与analysis_id复合关联analysis_reports'),
        col('item_key',code(160),'报告内复核条目标识：发现推断、方向或具体反证的稳定路径，不是全局身份'),
        col('candidate_hash',code(64),'被审核候选内容规范JSON的SHA256；内容变化拒绝沿用'),
        col('source_hash',code(64),'报告来源快照及明细版本规范JSON的SHA256；来源变化拒绝沿用'),
        col('revision',sa.Integer(),'同报告同条目决定序号：从1递增，乐观锁防止旧页面覆盖'),
        col('decision',code(20),'人工决定：pending待复核、adopted已采用、rejected不采用；pending可撤回旧决定'),
        col('reviewer',sa.String(80),'本机填写人说明：非空人工输入，不代表账户认证'),
        col('reason',sa.Text(),'人工审核意见：必填，不改写原AI返回或评价'),
        col('review_context',mysql.JSON(),'明确复核声明：采用时evidence_checked=true；方向另含conditions_checked=true及stop_scope，其他决定为空对象'),
        col('created_at',mysql.DATETIME(fsp=6),'明确保存决定的北京时间UTC+08:00'),
        sa.PrimaryKeyConstraint('id'),comment='通用分析逐项复核记录：追加保存人工决定及来源/内容身份，不自动确认AI语义',
        mysql_engine='InnoDB',mysql_charset='utf8mb4',mysql_collate='utf8mb4_0900_bin')
    t.append_constraint(sa.ForeignKeyConstraint(['analysis_id'],['analysis_tasks.id'],name='fk_analysis_decision_task',ondelete='RESTRICT',onupdate='RESTRICT'))
    t.append_constraint(sa.ForeignKeyConstraint(['report_id','analysis_id'],['analysis_reports.id','analysis_reports.analysis_id'],name='fk_analysis_decision_report',ondelete='RESTRICT',onupdate='RESTRICT'))
    t.append_constraint(sa.UniqueConstraint('report_id','item_key','revision',name='uq_analysis_decision_revision'))
    t.append_constraint(sa.CheckConstraint("decision IN ('pending','adopted','rejected')",name='ck_analysis_decision_state'))
    t.append_constraint(sa.CheckConstraint('revision > 0',name='ck_analysis_decision_revision'))
    sa.Index('ix_analysis_decision_lookup',t.c.analysis_id,t.c.report_id,t.c.item_key,t.c.revision)
    return m
