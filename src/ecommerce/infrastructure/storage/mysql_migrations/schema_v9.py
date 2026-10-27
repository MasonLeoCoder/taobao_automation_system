"""第九版冻结模型：统一采集申请、数据库队列与任务控制。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from ecommerce.infrastructure.storage.mysql_migrations.schema_v8 import build_metadata as previous_metadata

REVISION = '0009_collection_jobs'
PREVIOUS_REVISION = '0008_collection_recovery'


def build_metadata():
    """构造统一作业模型；参数：无；返回：含原生中文备注的元数据。"""
    metadata = previous_metadata()
    metadata.remove(metadata.tables['collection_retry_queue'])
    code = lambda n=32: sa.String(n, collation='ascii_bin')
    stamp = lambda name, comment, nullable=False: sa.Column(name, mysql.DATETIME(fsp=6), nullable=nullable, comment=comment)
    options = dict(mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_bin')
    jobs = sa.Table('collection_jobs', metadata,
        sa.Column('id', code(), primary_key=True, comment='采集申请编号：32位小写UUID；新执行沿用此编号，申请可先于tasks存在'),
        sa.Column('execution_key', code(64), nullable=False, comment='浏览器资源SHA256：按规范化资料目录隔离队列'),
        sa.Column('submission_key', code(), nullable=True, comment='提交幂等编号：同资源重复提交返回原申请；旧迁移记录可为空，结束后不清空'),
        sa.Column('request_key', code(64), nullable=True, comment='在途参数SHA256：相同范围合并；执行结束或启动失败后清空，允许主动重采'),
        sa.Column('origin', code(), nullable=False, comment='申请来源：web网页、cli命令行、legacy已有派发记录迁移'),
        sa.Column('dispatch_state', code(), nullable=False, comment='派发状态：waiting等待、paused暂停、claimed已领取、started已建执行、start_failed启动失败、cancelled启动前取消、interrupted启动中断、legacy_finished旧申请已结束'),
        sa.Column('task_id', code(), nullable=True, comment='实际执行编号：外键tasks.id，未执行申请为空；实际执行状态只从tasks读取'),
        sa.Column('claim_token', code(), nullable=True, comment='本次领取令牌：旧进程不得使用失效令牌启动或更新申请；未领取或旧记录可为空'),
        stamp('claimed_at', '领取时间：北京时间UTC+08:00；尚未领取为空', True),
        sa.Column('owner_pid', mysql.BIGINT(), nullable=True, comment='启动或执行进程编号：与启动时间一起核对，未启动为空'),
        stamp('owner_process_started_at', '进程启动时间：北京时间UTC+08:00；未启动或无法核实为空', True),
        sa.Column('payload', mysql.JSON(), nullable=False, comment='固定申请与已确认展示进度：mode/product_id/module/modules/options/config/resume_task_id/cleanup_tasks；配置仅含密码环境变量名'),
        sa.Column('message', sa.Text(), nullable=True, comment='申请排队或启动说明；实际执行结论读取tasks.message'),
        sa.Column('prompt', mysql.JSON(), nullable=True, comment='当前人工提示及存储等待状态：prompt_id/allowed/message/step/halted/storage_pending；无提示为空'),
        stamp('stop_requested_at', '用户停止请求时间：北京时间UTC+08:00；无停止请求为空，收到后不能自动启动', True),
        sa.Column('revision', mysql.BIGINT(unsigned=True), nullable=False, comment='申请递增版本：数据库变更时加一，控制与领取在行锁内核对'),
        stamp('created_at', '申请创建时间：北京时间UTC+08:00，微秒精度'),
        stamp('updated_at', '申请最近更新时间：北京时间UTC+08:00，微秒精度'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_collection_job_task', ondelete='RESTRICT', onupdate='RESTRICT'),
        sa.UniqueConstraint('task_id', name='uq_collection_job_task'),
        sa.UniqueConstraint('execution_key', 'submission_key', name='uq_collection_submission'),
        sa.UniqueConstraint('execution_key', 'request_key', name='uq_collection_active_request'),
        sa.CheckConstraint("origin IN ('web','cli','legacy')", name='ck_collection_job_origin'),
        sa.CheckConstraint("dispatch_state IN ('waiting','paused','claimed','started','start_failed','cancelled','interrupted','legacy_finished')", name='ck_collection_dispatch_state'),
        comment='统一采集作业表：申请与排队唯一来源，实际执行状态关联tasks，网页不读取派发文件', **options)
    sa.Index('ix_collection_job_queue', jobs.c.execution_key, jobs.c.dispatch_state, jobs.c.created_at, jobs.c.id)
    sa.Index('ix_collection_job_recent', jobs.c.execution_key, jobs.c.created_at, jobs.c.id)
    sa.Table('collection_queue_states', metadata,
        sa.Column('execution_key', code(64), primary_key=True, comment='浏览器资源SHA256：与collection_jobs.execution_key逻辑关联，无外键，空队列也保留'),
        sa.Column('paused', sa.Boolean(), nullable=False, comment='是否暂停自动领取：1暂停、0允许；不停止已经运行的任务'),
        sa.Column('reason', sa.Text(), nullable=True, comment='暂停或恢复原因；从未暂停可为空'),
        sa.Column('revision', mysql.BIGINT(unsigned=True), nullable=False, comment='队列控制递增版本：资源行锁串行修改'),
        stamp('updated_at', '队列控制更新时间：北京时间UTC+08:00'),
        comment='采集队列资源状态表：停止及重启暂停持久保存，不依赖队列是否为空', **options)
    controls = sa.Table('collection_job_controls', metadata,
        sa.Column('id', code(), primary_key=True, comment='控制指令编号：32位小写UUID，补写沿用原编号'),
        sa.Column('job_id', code(), nullable=False, comment='目标申请：外键collection_jobs.id，不能作用于其他任务'),
        sa.Column('claim_token', code(), nullable=True, comment='目标执行领取令牌：与申请核对；旧记录无令牌时为空'),
        sa.Column('prompt_id', code(), nullable=True, comment='反馈对应人工提示编号：继续/确认/取消必须匹配；停止或暂停为空'),
        sa.Column('action', code(), nullable=False, comment='控制动作：stop停止、pause人工暂停、continue继续、confirm确认、cancel取消原生弹窗'),
        sa.Column('state', code(), nullable=False, comment='指令状态：pending等待接收、consumed已接收、rejected已过期或不匹配；接收不等于平台验证通过'),
        stamp('created_at', '指令提交时间：北京时间UTC+08:00'),
        stamp('handled_at', '指令接收或拒绝时间：北京时间UTC+08:00；待接收为空', True),
        sa.ForeignKeyConstraint(['job_id'], ['collection_jobs.id'], name='fk_collection_control_job', ondelete='RESTRICT', onupdate='RESTRICT'),
        sa.CheckConstraint("action IN ('stop','pause','continue','confirm','cancel')", name='ck_collection_control_action'),
        sa.CheckConstraint("state IN ('pending','consumed','rejected')", name='ck_collection_control_state'),
        comment='采集任务控制指令表：绑定任务和当前提示，数据库保存申请及接收记录', **options)
    sa.Index('ix_collection_control_pending', controls.c.job_id, controls.c.state, controls.c.created_at, controls.c.id)
    return metadata
