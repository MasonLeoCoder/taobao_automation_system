"""单元测试使用同版模型与真实存储方法，SQLite仅替代连接及资源行初始化语法。"""
from types import SimpleNamespace
from unittest.mock import patch
import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from ecommerce.infrastructure.storage.task_store import MySQLTaskStore
from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
from ecommerce.infrastructure.storage.mysql_migrations.schema_v9 import build_metadata
from ecommerce.infrastructure.storage.time_values import database_now, database_time


class JobDatabaseFixture:
    """隔离每个测试的申请数据库；MySQL锁和迁移另外由集成测试验证。"""
    def __init__(self, case, settings):
        self.settings=settings
        self.tables=build_metadata().tables
        self.engine=sa.create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=sa.pool.StaticPool)
        @sa.event.listens_for(self.engine,'connect')
        def configure(connection,record):
            """仅为测试注册排序并开启外键；参数：连接、记录；返回：无。"""
            connection.create_collation('ascii_bin',lambda a,b:(a>b)-(a<b))
            connection.execute('PRAGMA foreign_keys=ON')
        self.tables['task_events'].c.id.type=sa.Integer()
        names=('tasks','task_events','collection_plans','collection_plan_executions','collection_jobs','collection_queue_states','collection_job_controls')
        self.tables['tasks'].metadata.create_all(self.engine,tables=[self.tables[n] for n in names])
        self.store=object.__new__(MySQLTaskStore)
        self.store.engine,self.store.tables,self.store.settings=self.engine,self.tables,settings.mysql
        self.store.initialize=lambda:None
        def factory(mysql):
            """不同测试数据库不会共享记录；参数：连接定位；返回：存储或不存在。"""
            if mysql.database!=settings.mysql.database:
                raise LookupError('该测试数据库没有此申请')
            return self.store
        case.addCleanup(self.engine.dispose)
        for target in ('ecommerce.infrastructure.storage.collection_jobs.task_store','ecommerce.infrastructure.storage.collection_recovery.task_store',
                       'ecommerce.infrastructure.storage.task_store.task_store'):
            mocked=patch(target,side_effect=factory);mocked.start();case.addCleanup(mocked.stop)
        original=CollectionJobStore.resource
        def resource(persistence,connection,key):
            """测试SQL方言适配；参数：存储、连接、资源；返回：资源行。"""
            if connection.dialect.name!='sqlite':
                return original(persistence,connection,key)
            q=persistence.tables['collection_queue_states']
            connection.execute(insert(q).values(execution_key=key,paused=False,reason=None,revision=1,updated_at=database_now()).on_conflict_do_nothing())
            return connection.execute(sa.select(q).where(q.c.execution_key==key)).mappings().one()
        mocked=patch.object(CollectionJobStore,'resource',resource);mocked.start();case.addCleanup(mocked.stop)

    def save(self, row):
        """模拟执行器已提交状态，不能由正式网页修改执行结论；参数：测试描述；返回：无。"""
        j,t=self.tables['collection_jobs'],self.tables['tasks']
        with self.engine.begin() as c:
            old=c.execute(sa.select(j).where(j.c.id==row['id'])).mappings().one()
            payload=dict(old['payload'])
            payload.update({k:row[k] for k in ('progress','recovery_ready','modules','skipped_modules') if k in row})
            state=row.get('state')
            changes={'payload':payload}
            if state in {'running','succeeded','failed','cancelled','needs_attention'}:
                finished=state not in {'running'}
                task=c.scalar(sa.select(t.c.id).where(t.c.id==row['id']))
                values={'status':state,'message':row.get('message'),'finished_at':database_now() if finished else None}
                if task:
                    c.execute(t.update().where(t.c.id==row['id']).values(**values))
                else:
                    c.execute(t.insert().values(id=row['id'],name='collection.taobao.synthetic',origin='live',started_at=database_now(),
                        execution_key=row['execution_key'],owner_pid=old['owner_pid'],owner_process_started_at=old['owner_process_started_at'],**values))
                changes.update(dispatch_state='started',task_id=row['id'],request_key=None if finished else old['request_key'])
            c.execute(j.update().where(j.c.id==row['id']).values(**changes))
            p,e=self.tables['collection_plans'],self.tables['collection_plan_executions']
            if row.get('recovery_ready') and not c.scalar(sa.select(e.c.task_id).where(e.c.task_id==row['id'])):
                c.execute(p.insert().values(id=row['id'],active_task_id=row['id'],format_version=1,options=row.get('options',{}),
                    checkpoint={'finished':False,'products':{},'pages':{}},revision=1,created_at=database_now(),updated_at=database_now()))
                c.execute(e.insert().values(task_id=row['id'],plan_id=row['id'],created_at=database_now()))

    def prompt(self, row, prompt):
        """直接准备已入库提示；参数：申请、提示；返回：无。"""
        j=self.tables['collection_jobs']
        with self.engine.begin() as c:
            c.execute(j.update().where(j.c.id==row['id']).values(prompt=prompt))
