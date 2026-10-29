"""第十一版分析模型：通用五表，首版业务仅启用评价分析。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from ecommerce.infrastructure.storage.mysql_migrations.schema_v10 import build_metadata as previous_metadata

REVISION = '0011_analysis'
PREVIOUS_REVISION = '0010_product_favorites'
NEW_TABLES = ('analysis_tasks', 'analysis_sources', 'analysis_calls', 'analysis_details', 'analysis_reports')


def build_metadata():
    """构建带中文备注的分析表；参数：无；返回：完整元数据，不修改旧版本。"""
    m = previous_metadata()
    number = lambda: mysql.BIGINT(unsigned=True)
    code = lambda n=32: sa.String(n, collation='ascii_bin')
    instant = lambda: mysql.DATETIME(fsp=6)

    def col(name, kind, comment, nullable=False):
        """声明字段；参数：名称、类型、中文含义及空值；返回：列。"""
        return sa.Column(name, kind, nullable=nullable, comment=comment)

    def table(name, comment, *columns):
        """声明表；参数：名称、用途、字段；返回：InnoDB表。"""
        return sa.Table(name, m, sa.Column('id', code(), primary_key=True, comment='记录编号：32位小写UUID，不是商品或采集编号'),
                        *columns, comment=comment, mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_bin')

    def fk(t, columns, targets, name):
        """添加保护来源的外键；参数：表、列、目标、名称；返回：无。"""
        t.append_constraint(sa.ForeignKeyConstraint(columns, targets, name=name, ondelete='RESTRICT', onupdate='RESTRICT'))

    tasks = table('analysis_tasks', '通用分析任务表：来源快照、版本、预算及运行状态；首版仅评价分析',
        col('product_id', number(), '商品内部编号：关联products.id，不是平台商品编号'),
        col('analysis_type', code(), '分析类型：首版仅reviews；其他类型未启用'),
        col('fingerprint', code(64), '复用指纹：输入快照、模型、提示、映射和关键参数的SHA256'),
        col('active_key', code(64), '活动互斥键：queued/running时等于fingerprint；其他状态SQL NULL', True),
        col('status', code(), '任务状态：queued待启动、running执行、succeeded完成、partial部分完成、failed失败、interrupted中断'),
        col('provider', code(), 'AI服务商：首版deepseek'), col('model', sa.String(100), '请求模型名称：创建时固定，不随默认配置改变'),
        col('rule_version', code(), '分析规则版本：reviews-v1'),
        col('rules', mysql.JSON(), '规则快照：schema_version=1、分类提示、报告提示、主题映射及统计口径；不含密钥'),
        col('parameters', mysql.JSON(), '参数快照：batch_size、max_output_tokens、max_calls、max_tokens、config_file引用；不含密钥'),
        col('source_count', sa.Integer(), '输入评价条数：包含初评与追评的一组算一条，空白单独标记'),
        col('processed_count', sa.Integer(), '已校验并保存明细条数；不是AI返回条数'),
        col('usage', mysql.JSON(), '累计已知用量：prompt_tokens、completion_tokens、total_tokens、unknown_calls；未知费用不记为零'),
        col('message', sa.Text(), '安全进度或错误说明：不包含密钥及上游原始异常'),
        col('owner', mysql.JSON(), '工作进程身份：pid、started_at；SQL NULL表示未派发或执行已结束', True),
        col('execution_task_id', code(), '最近执行的通用任务编号：关联tasks.id；尚未执行时SQL NULL', True),
        col('created_at', instant(), '创建时间：北京时间UTC+08:00'),
        col('updated_at', instant(), '最近状态变更时间：北京时间UTC+08:00'))
    tasks.append_constraint(sa.UniqueConstraint('active_key', name='uq_analysis_active'))
    tasks.append_constraint(sa.CheckConstraint("status IN ('queued','running','succeeded','partial','failed','interrupted')", name='ck_analysis_status'))
    tasks.append_constraint(sa.CheckConstraint('source_count >= 0 AND processed_count >= 0 AND processed_count <= source_count', name='ck_analysis_counts'))
    tasks.append_constraint(sa.CheckConstraint("(status IN ('queued','running') AND active_key IS NOT NULL AND active_key = fingerprint) OR (status NOT IN ('queued','running') AND active_key IS NULL)", name='ck_analysis_active'))
    fk(tasks, ['product_id'], ['products.id'], 'fk_analysis_product')
    fk(tasks, ['execution_task_id'], ['tasks.id'], 'fk_analysis_execution')
    sa.Index('ix_analysis_product', tasks.c.product_id, tasks.c.created_at, tasks.c.id)
    sa.Index('ix_analysis_fingerprint', tasks.c.fingerprint)
    sources = table('analysis_sources', '分析来源表：保存明确评价关联及脱离后续采集变化的输入快照',
        col('analysis_id', code(), '所属分析编号：关联analysis_tasks.id'),
        col('batch_id', number(), '原评价批次：关联collection_batches.id；业务校验已发布且同商品'),
        col('review_id', number(), '原评价观察编号：与batch_id复合关联review_observations'),
        col('position', sa.Integer(), '本次输入顺序：从1开始，同文评价不自动删除'),
        col('snapshot', mysql.JSON(), '输入快照：id、text、sku、appends[{segment,text}]；不含昵称媒体URL'),
        col('content_hash', code(64), '输入快照规范化JSON的SHA256'),
        col('scope', mysql.JSON(), '来源范围：采集时间、数量上限、覆盖状态、停止原因及追评条数；不发送AI'))
    fk(sources, ['analysis_id'], ['analysis_tasks.id'], 'fk_analysis_source_task')
    fk(sources, ['batch_id'], ['collection_batches.id'], 'fk_analysis_source_batch')
    fk(sources, ['review_id', 'batch_id'], ['review_observations.id', 'review_observations.batch_id'], 'fk_analysis_source_review')
    sources.append_constraint(sa.UniqueConstraint('analysis_id', 'position', name='uq_analysis_source_position'))
    sources.append_constraint(sa.UniqueConstraint('analysis_id', 'review_id', name='uq_analysis_source_review'))
    sources.append_constraint(sa.CheckConstraint('position > 0', name='ck_analysis_source_position'))
    calls = table('analysis_calls', '分析AI调用表：每次尝试先入库再联网，无自动重试，原始返回与用量可追溯',
        col('analysis_id', code(), '所属分析编号：关联analysis_tasks.id'),
        col('kind', code(), '调用用途：classify逐条分类、report综合发现与建议'),
        col('chunk_index', sa.Integer(), '逻辑分批序号：分类从1开始，报告为0；重发生成新记录'),
        col('status', code(), '调用状态：inflight已准备联网、succeeded校验通过、invalid返回不合格、unknown结果未知'),
        col('request', mysql.JSON(), '请求快照：prompt与source_ids及输出上限，不包含HTTP头或密钥'),
        col('response', mysql.JSON(), '原始返回：客户端安全文字、finish_reason、模型标识、用量；无返回时SQL NULL', True),
        col('usage', mysql.JSON(), '本次已知用量及reserved_tokens保守预算；无响应时unknown=true'),
        col('error', sa.Text(), '安全错误或校验说明：空字符串表示无错误'),
        col('started_at', instant(), '调用准备时间：北京时间UTC+08:00'),
        col('finished_at', instant(), '返回及校验完成时间：北京时间UTC+08:00；未完成SQL NULL', True))
    fk(calls, ['analysis_id'], ['analysis_tasks.id'], 'fk_analysis_call_task')
    calls.append_constraint(sa.CheckConstraint("status IN ('inflight','succeeded','invalid','unknown')", name='ck_analysis_call_status'))
    calls.append_constraint(sa.CheckConstraint("(kind = 'classify' AND chunk_index > 0) OR (kind = 'report' AND chunk_index = 0)", name='ck_analysis_call_kind'))
    sa.Index('ix_analysis_call_chunk', calls.c.analysis_id, calls.c.kind, calls.c.chunk_index)
    details = table('analysis_details', '分析逐条结果表：保留原始分类、人工修订历史及排除依据',
        col('analysis_id', code(), '所属分析编号：关联analysis_tasks.id'),
        col('source_id', code(), '来源快照编号：关联analysis_sources.id，同任务由业务核验'),
        col('call_id', code(), '产生分类的调用编号：关联analysis_calls.id，同任务由业务核验'),
        col('result', mysql.JSON(), '校验分类：schema_version=1、kind、points[{topic,sentiment,segment,quote}]；含unclear不等于通过人工复核'),
        col('corrections', mysql.JSON(), '追加修订历史：[{revision,reason,time,before,after}]；空数组表示未修订，密钥禁止写入'),
        col('revision', sa.Integer(), '乐观锁版本：初始0，每次人工修订加1，防止旧页面覆盖'),
        col('review_status', code(), '复核状态：unreviewed未复核、corrected已人工修正；不自动认定整份报告已审核'))
    fk(details, ['analysis_id'], ['analysis_tasks.id'], 'fk_analysis_detail_task')
    fk(details, ['source_id'], ['analysis_sources.id'], 'fk_analysis_detail_source')
    fk(details, ['call_id'], ['analysis_calls.id'], 'fk_analysis_detail_call')
    details.append_constraint(sa.UniqueConstraint('source_id', name='uq_analysis_detail_source'))
    details.append_constraint(sa.CheckConstraint('revision >= 0', name='ck_analysis_detail_revision'))
    reports = table('analysis_reports', '分析报告版本表：本地统计、可追溯发现及建议，重统计不覆盖旧报告',
        col('analysis_id', code(), '所属分析编号：关联analysis_tasks.id'),
        col('version', sa.Integer(), '报告版本：同任务从1递增，人工修订后重新统计形成新版本'),
        col('content', mysql.JSON(), '报告结构：schema_version=1、statistics、findings、suggestions、issues、detail_revisions、coverage、review_status'),
        col('created_at', instant(), '报告生成时间：北京时间UTC+08:00'))
    fk(reports, ['analysis_id'], ['analysis_tasks.id'], 'fk_analysis_report_task')
    reports.append_constraint(sa.UniqueConstraint('analysis_id', 'version', name='uq_analysis_report_version'))
    reports.append_constraint(sa.CheckConstraint('version > 0', name='ck_analysis_report_version'))
    return m
