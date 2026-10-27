"""将补采队列升级为统一申请队列；不改写采集业务资料。"""
from alembic import op
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v9 import build_metadata

revision = '0009_collection_jobs'
down_revision = '0008_collection_recovery'
branch_labels = None
depends_on = None


def upgrade():
    """幂等迁移队列结构和原队列记录；参数：无；返回：无。"""
    connection = op.get_bind()
    if connection.scalar(sa.text('SELECT COUNT(*) FROM tasks WHERE finished_at IS NULL')):
        raise RuntimeError('存在未结束任务，暂不升级统一采集队列')
    metadata = build_metadata()
    for name in ('collection_jobs', 'collection_queue_states', 'collection_job_controls'):
        metadata.tables[name].create(connection, checkfirst=True)
    if sa.inspect(connection).has_table('collection_retry_queue'):
        old = sa.Table('collection_retry_queue', sa.MetaData(), autoload_with=connection)
        jobs, queues = metadata.tables['collection_jobs'], metadata.tables['collection_queue_states']
        for row in connection.execute(sa.select(old)).mappings():
            if connection.scalar(sa.select(jobs.c.id).where(jobs.c.id == row['id'])):
                continue
            terminal = row['state'] in {'done', 'cancelled'}
            task_id = connection.scalar(sa.text('SELECT id FROM tasks WHERE id=:id'), {'id': row['id']})
            connection.execute(jobs.insert().values(id=row['id'], execution_key=row['execution_key'],
                submission_key=None, request_key=None if terminal else row['request_key'], origin='legacy',
                dispatch_state='started' if task_id else ('cancelled' if row['state']=='cancelled' else 'legacy_finished' if terminal else 'paused'),
                task_id=task_id, payload={'legacy_queue':dict(row['payload']), **row['payload'], 'migration_pending':True},
                message='旧补采队列已迁移，待核对配置；不会自动启动', revision=1,
                created_at=row['created_at'], updated_at=row['updated_at']))
            if not terminal and not connection.scalar(sa.select(queues.c.execution_key).where(queues.c.execution_key==row['execution_key'])):
                connection.execute(queues.insert().values(execution_key=row['execution_key'],paused=True,
                    reason='旧队列迁移，等待手动核对',revision=1,updated_at=row['updated_at']))
        missing = connection.scalar(sa.select(sa.func.count()).select_from(old.outerjoin(jobs, old.c.id==jobs.c.id)).where(jobs.c.id.is_(None)))
        if missing:
            raise RuntimeError('旧补采队列尚有未迁移记录，保留旧表')
        old.drop(connection)


def downgrade():
    """拒绝自动丢弃申请及控制凭据；参数：无；返回：异常。"""
    raise RuntimeError('统一申请和控制记录不可自动删除，回退须单独审核')
