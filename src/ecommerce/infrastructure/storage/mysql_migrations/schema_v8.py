"""第八版冻结模型：采集计划、恢复检查点、清理凭据与本机补采队列。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from ecommerce.infrastructure.storage.mysql_migrations.schema_v7 import build_metadata as previous_metadata

REVISION = '0008_collection_recovery'
PREVIOUS_REVISION = '0007_basic_info'


def build_metadata():
    """构造恢复模型；参数：无；返回：包含中文备注及约束的元数据。"""
    metadata = previous_metadata()
    code = lambda n=32: sa.String(n, collation='ascii_bin')
    stamp = lambda name, comment: sa.Column(name, mysql.DATETIME(fsp=6), nullable=False, comment=comment)
    options = dict(mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_bin')
    plans = sa.Table('collection_plans', metadata,
        sa.Column('id', code(), primary_key=True, comment='采集计划编号：首次执行的 tasks.id，恢复执行共用原计划'),
        sa.Column('active_task_id', code(), nullable=False, comment='当前或最近执行任务：关联 tasks.id，资源锁内串行领取'),
        sa.Column('format_version', sa.Integer(), nullable=False, comment='恢复契约版本：当前为1，旧任务缺少计划时不猜测恢复'),
        sa.Column('options', mysql.JSON(), nullable=False, comment='不可变原计划：mode、keyword/pages/limit/with_details，或input/modules/all_reviews/all_questions'),
        sa.Column('checkpoint', mysql.JSON(), nullable=False, comment='累计检查点：pages已保存列表、products模块处理结果、counts数量；只复用本计划资料'),
        sa.Column('revision', mysql.BIGINT(unsigned=True), nullable=False, comment='检查点递增版本：与active_task_id共同拒绝旧流水覆盖新执行'),
        stamp('created_at', '计划创建时间：北京时间UTC+08:00，微秒精度'),
        stamp('updated_at', '检查点更新时间：北京时间UTC+08:00，微秒精度'),
        sa.ForeignKeyConstraint(['id'], ['tasks.id'], name='fk_plan_root_task', ondelete='RESTRICT', onupdate='RESTRICT'),
        sa.ForeignKeyConstraint(['active_task_id'], ['tasks.id'], name='fk_plan_active_task', ondelete='RESTRICT', onupdate='RESTRICT'),
        comment='采集恢复计划表：保存原范围和已确认检查点，不用商品全局最新资料推断原任务进度', **options)
    sa.Table('collection_plan_executions', metadata,
        sa.Column('task_id', code(), primary_key=True, comment='本次执行编号：关联tasks.id，每次恢复新建执行'),
        sa.Column('plan_id', code(), nullable=False, comment='原采集计划：关联collection_plans.id'),
        sa.Column('parent_task_id', code(), nullable=True, comment='恢复来源执行：关联tasks.id，首次执行为空'),
        stamp('created_at', '执行关联建立时间：北京时间UTC+08:00'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_plan_execution_task', ondelete='RESTRICT', onupdate='RESTRICT'),
        sa.ForeignKeyConstraint(['plan_id'], ['collection_plans.id'], name='fk_execution_plan', ondelete='RESTRICT', onupdate='RESTRICT'),
        sa.ForeignKeyConstraint(['parent_task_id'], ['tasks.id'], name='fk_execution_parent', ondelete='RESTRICT', onupdate='RESTRICT'),
        comment='采集计划执行关联表：连接首次执行及各次恢复，保留原任务结束结论', **options)
    sa.Table('collection_discarded_batches', metadata,
        sa.Column('source_batch_key', code(96), primary_key=True, comment='已废弃未完成批次原编号：阻止旧快照重新写回，原批次元数据保留'),
        sa.Column('source_task_id', code(), nullable=False, comment='旧批次所属任务：关联tasks.id'),
        sa.Column('cleanup_task_id', code(), nullable=False, comment='发起清理任务：关联tasks.id'),
        sa.Column('product_id', code(25), nullable=False, comment='淘宝平台商品编号：字符串，不是products.id'),
        sa.Column('module', code(), nullable=False, comment='清理的单一详情模块，不能跨商品或模块删除'),
        sa.Column('stage', code(), nullable=False, comment='清理阶段：records_deleted数据库清理完成、complete专属文件清理核对完成'),
        sa.Column('manifest', mysql.JSON(), nullable=False, comment='不可变清理文件清单：相对路径、长度和SHA256；共享文件排除，禁止递归猜测删除'),
        sa.Column('counts', mysql.JSON(), nullable=False, comment='实际删除数量：各明细表行数、files文件数及shared共享保留数量'),
        stamp('created_at', '废弃登记时间：北京时间UTC+08:00'),
        stamp('updated_at', '清理阶段更新时间：北京时间UTC+08:00'),
        sa.ForeignKeyConstraint(['source_task_id'], ['tasks.id'], name='fk_discard_source_task', ondelete='RESTRICT', onupdate='RESTRICT'),
        sa.ForeignKeyConstraint(['cleanup_task_id'], ['tasks.id'], name='fk_discard_cleanup_task', ondelete='RESTRICT', onupdate='RESTRICT'),
        sa.CheckConstraint("stage IN ('records_deleted','complete')", name='ck_discard_stage'),
        comment='未完成采集批次清理凭据表：保留审计和防回流标记，明细删除后新批次重新采集', **options)
    queue = sa.Table('collection_retry_queue', metadata,
        sa.Column('id', code(), primary_key=True, comment='补采派发编号：启动后与tasks.id相同，排队时尚无任务行'),
        sa.Column('execution_key', code(64), nullable=False, comment='浏览器执行资源键：与所属配置一致，不跨资料目录派发'),
        sa.Column('request_key', code(64), nullable=True, comment='在途请求指纹：配置和商品模块范围相同则合并；终态清空允许再次补采'),
        sa.Column('state', code(), nullable=False, comment='队列状态：waiting等待、paused暂停、claimed已领取、done结束、cancelled取消'),
        sa.Column('payload', mysql.JSON(), nullable=False, comment='固定补采派发配置及清理来源：含商品、模块、数量范围和原任务关联，不含密码'),
        stamp('created_at', '补采申请时间：北京时间UTC+08:00'),
        stamp('updated_at', '队列状态更新时间：北京时间UTC+08:00'),
        sa.UniqueConstraint('execution_key', 'request_key', name='uq_retry_active_request'),
        sa.CheckConstraint("state IN ('waiting','paused','claimed','done','cancelled')", name='ck_retry_queue_state'),
        comment='本机补采队列表：持久排队及重复请求合并；停止、浏览器关闭及服务重启后保持暂停', **options)
    sa.Index('ix_retry_resource_state', queue.c.execution_key, queue.c.state, queue.c.created_at)
    return metadata
