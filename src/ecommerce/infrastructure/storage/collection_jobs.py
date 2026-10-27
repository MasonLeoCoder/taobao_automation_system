"""统一采集申请与控制存储；不执行浏览器动作，不读取派发文件。"""
from contextlib import contextmanager
from copy import deepcopy
import re
from uuid import uuid4
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error
from ecommerce.infrastructure.storage.task_store import task_store, fingerprint
from ecommerce.infrastructure.storage.time_values import database_now, database_time, iso_value
from ecommerce.shared.exceptions import NeedsAttention

PENDING = ('waiting', 'paused', 'claimed')
TERMINAL = ('start_failed', 'cancelled', 'interrupted', 'legacy_finished')
HISTORY_STATUSES = ('all', 'waiting', 'running', 'attention', 'stopped', 'completed', 'failed')


class JobConflict(ValueError, NeedsAttention):
    """申请或领取归属变化，必须重新查询，不能自动重放。"""
    retryable = False


def identifier(value):
    """校验公开编号；参数：字符串；返回：原编号。"""
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('采集申请编号无效')
    return value


def attach_execution(connection, tables, operation):
    """在创建任务同一事务内校验领取；参数：事务、模型、创建操作；返回：无。"""
    j=tables['collection_jobs']
    row=connection.execute(sa.select(j).where(j.c.id==operation['task_id']).with_for_update()).mappings().first()
    if row is None:
        return True
    if (row['dispatch_state']!='claimed' or not operation.get('job_claim_token')
            or row['claim_token']!=operation['job_claim_token'] or row['stop_requested_at'] is not None):
        # 创建入库前已取消或领取失效，确认作废流水，不制造tasks；启动方仍须核对关联。
        return False
    return True


def complete_attachment(connection, tables, operation):
    """任务插入后建立外键关联；参数：事务、模型、创建操作；返回：无。"""
    if not operation.get('job_claim_token'):
        return
    j=tables['collection_jobs']
    connection.execute(j.update().where(j.c.id==operation['task_id'],j.c.dispatch_state=='claimed').values(
        dispatch_state='started',task_id=operation['task_id'],owner_pid=operation['task']['owner_pid'],
        owner_process_started_at=database_time(operation['task'].get('owner_process_started_at')),
        message='已交给统一执行器',revision=j.c.revision+1,updated_at=database_now()))


def apply_runtime(connection, tables, operation):
    """与事件一起确认展示进度和提示；参数：事务、模型、持久操作；返回：无。"""
    j=tables['collection_jobs']
    old=connection.execute(sa.select(j).where(j.c.id==operation['task_id']).with_for_update()).mappings().one()
    if old['claim_token']!=operation.get('job_claim_token'):
        raise JobConflict('旧执行不能更新申请进度')
    values={'revision':old['revision']+1,'updated_at':database_now()}
    if operation['op']=='job_prompt':
        values['prompt']=operation.get('prompt')
    else:
        payload=deepcopy(old['payload'])
        payload.update({k:v for k,v in operation['runtime'].items() if k in {'progress','recovery_ready'}})
        values['payload']=payload
    connection.execute(j.update().where(j.c.id==operation['task_id']).values(**values))


def finish_execution(connection, tables, task_id, status):
    """结论与去重释放、队列暂停同事务；参数：事务、模型、任务、结论；返回：无。"""
    j,q,k=(tables[name] for name in ('collection_jobs','collection_queue_states','collection_job_controls'))
    row=connection.execute(sa.select(j).where(j.c.task_id==task_id).with_for_update()).mappings().first()
    if row is None:
        return
    connection.execute(j.update().where(j.c.id==row['id']).values(request_key=None,prompt=None,revision=j.c.revision+1,updated_at=database_now()))
    connection.execute(k.update().where(k.c.job_id==row['id'],k.c.state=='pending').values(
        state=sa.case((k.c.action=='stop','consumed'),else_='rejected'),handled_at=database_now()))
    if status!='succeeded':
        connection.execute(q.update().where(q.c.execution_key==row['execution_key']).values(paused=True,
            reason='采集已停止或需要核对，等待手动继续队列',revision=q.c.revision+1,updated_at=database_now()))
        connection.execute(j.update().where(j.c.execution_key==row['execution_key'],j.c.dispatch_state=='waiting').values(
            dispatch_state='paused',revision=j.c.revision+1,updated_at=database_now()))


class CollectionJobStore:
    """复用任务连接池，资源行锁保证申请、领取及暂停串行。"""
    def __init__(self, settings):
        self.settings = settings
        self.tasks = task_store(settings.mysql)
        self.engine, self.tables = self.tasks.engine, self.tasks.tables

    @contextmanager
    def connection(self, write=False):
        """转换数据库异常；参数：是否写事务；返回：连接。"""
        try:
            with (self.engine.begin() if write else self.engine.connect()) as connection:
                yield connection
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None

    def resource(self, connection, key):
        """创建并锁定资源状态；参数：事务连接、资源键；返回：资源行。"""
        q = self.tables['collection_queue_states']
        statement = insert(q).values(execution_key=key, paused=False, reason=None, revision=1, updated_at=database_now())
        connection.execute(statement.on_duplicate_key_update(execution_key=statement.inserted.execution_key))
        return connection.execute(sa.select(q).where(q.c.execution_key==key).with_for_update()).mappings().one()

    def submit(self, row, *, submission_key=None, origin='web'):
        """原子保存固定申请并合并重复提交；参数：申请、提交编号、来源；返回：申请编号。"""
        j = self.tables['collection_jobs']
        identifier(row['id'])
        if submission_key is not None:
            identifier(submission_key)
        spec = {key:row.get(key) for key in ('product_id','module','mode','modules','options','resume_task_id','resume_revision','cleanup_tasks')}
        digest = fingerprint(spec)
        with self.connection(True) as c:
            queue = self.resource(c, row['execution_key'])
            if submission_key:
                previous = c.execute(sa.select(j).where(j.c.execution_key==row['execution_key'],j.c.submission_key==submission_key)).mappings().first()
                if previous:
                    if previous['payload'].get('request_spec') != spec:
                        raise JobConflict('同一个提交编号不能改变采集参数，请重新提交')
                    return previous['id']
            previous = c.scalar(sa.select(j.c.id).where(j.c.execution_key==row['execution_key'],j.c.request_key==digest))
            if previous:
                return previous
            payload = deepcopy(row)
            payload['request_spec'] = spec
            for key in ('state','task_status','result','owner_pid','owner_process_started_at','message','created_at','updated_at'):
                payload.pop(key, None)
            c.execute(j.insert().values(id=row['id'],execution_key=row['execution_key'],submission_key=submission_key,
                request_key=digest,origin=origin,dispatch_state='paused' if queue['paused'] else 'waiting',
                payload=payload,message='队列已暂停，等待手动继续' if queue['paused'] else '采集申请已入库，等待执行',
                revision=1,created_at=database_time(row['created_at']),updated_at=database_now()))
        return row['id']

    def _statement(self):
        """构造实际执行与申请联合查询；参数：无；返回：查询。"""
        j,t,e = (self.tables[name] for name in ('collection_jobs','tasks','collection_plan_executions'))
        return sa.select(j,t.c.status.label('actual_status'),t.c.finished_at.label('actual_finished_at'),
            t.c.message.label('actual_message'),t.c.owner_pid.label('actual_pid'),
            t.c.owner_process_started_at.label('actual_process_started_at'),
            sa.exists(sa.select(e.c.task_id).where(e.c.task_id==j.c.task_id)).label('has_plan')).outerjoin(t,j.c.task_id==t.c.id)

    def _public_row(self, record):
        """还原内部兼容描述，执行结论只取任务表；参数：查询行；返回：描述。"""
        record = dict(record)
        row = deepcopy(record['payload'])
        state = record['dispatch_state']
        row.update(id=record['id'],execution_key=record['execution_key'],database=self.settings.mysql.database,
            dispatch_state=state,claim_token=record['claim_token'],task_id=record['task_id'],origin=record['origin'],
            owner_pid=record['actual_pid'] or record['owner_pid'],
            owner_process_started_at=iso_value(record['actual_process_started_at'] or record['owner_process_started_at']),
            created_at=iso_value(record['created_at']),updated_at=iso_value(record['updated_at']),
            message=record['actual_message'] or record['message'] or '',prompt=record['prompt'],
            stop_requested=record['stop_requested_at'] is not None,revision=record['revision'],
            recovery_ready=bool(record['has_plan']),actual_finished=record['actual_finished_at'] is not None)
        row.setdefault('product_id',None)
        row.setdefault('module','selected')
        from ecommerce.shared.collection_contract import MODULES
        if not row.get('mode'):
            row['mode']='single' if row['module'] in MODULES else 'detail'
        row.setdefault('modules',[row['module']] if row['mode']=='single' else [])
        if 'history_status' in record:
            row['history_status']=record['history_status']
        if state == 'started':
            row['state'] = record['actual_status'] or 'unverified'
            row['task_status'] = record['actual_status']
        else:
            row['state'] = {'waiting':'queued','paused':'paused','claimed':'starting','start_failed':'failed',
                'cancelled':'cancelled','interrupted':'interrupted','legacy_finished':row.get('legacy_state','interrupted')}[state]
        return row

    def read(self, job_id):
        """读取申请与实际执行；参数：编号；返回：内部描述或不存在异常。"""
        identifier(job_id)
        with self.connection() as c:
            row = c.execute(self._statement().where(self.tables['collection_jobs'].c.id==job_id)).mappings().first()
        if row is None:
            raise LookupError('采集申请不存在')
        return self._public_row(row)

    def submitted(self, key, submission_key):
        """按提交编号查找已确认申请；参数：资源和提交编号；返回：记录或空。"""
        identifier(submission_key)
        j=self.tables['collection_jobs']
        with self.connection() as c:
            row=c.execute(self._statement().where(j.c.execution_key==key,j.c.submission_key==submission_key)).mappings().first()
        return self._public_row(row) if row else None

    def rows(self, execution_key=None, *, limit=200, pending=False):
        """有界读取资源申请；参数：资源、上限、是否只读在途；返回：最近记录。"""
        j,t = self.tables['collection_jobs'],self.tables['tasks']
        query = self._statement()
        if execution_key:
            query = query.where(j.c.execution_key==execution_key)
        if pending:
            query = query.where(sa.or_(j.c.dispatch_state.in_(PENDING),sa.and_(j.c.dispatch_state=='started',t.c.finished_at.is_(None))))
            # 在途查询优先保留已领取/执行项，避免大量等待申请挤掉底部运行状态。
            query = query.order_by(sa.case((j.c.dispatch_state.in_(('claimed','started')),0),else_=1))
        with self.connection() as c:
            return [self._public_row(row) for row in c.execute(query.order_by(j.c.created_at.desc(),j.c.id.desc()).limit(limit)).mappings()]

    def queue_state(self, key):
        """读取空队列仍保留的暂停状态；参数：资源键；返回：状态。"""
        q = self.tables['collection_queue_states']
        with self.connection() as c:
            row = c.execute(sa.select(q).where(q.c.execution_key==key)).mappings().first()
            return {'paused':bool(row and row['paused']),'reason':row['reason'] if row else None}

    def history_status(self):
        """按入库申请、执行结论和人工提示归类；参数：无；返回：SQL状态表达式。"""
        j,t=self.tables['collection_jobs'],self.tables['tasks']
        legacy=j.c.payload['legacy_state'].as_string()
        attention=sa.or_(j.c.prompt['prompt_id'].as_string().is_not(None),
            j.c.prompt['halted'].as_boolean()==True,j.c.prompt['storage_pending'].as_boolean()==True)
        return sa.case(
            (j.c.dispatch_state.in_(('cancelled','interrupted')),'stopped'),
            (j.c.dispatch_state=='start_failed','failed'),
            (sa.and_(j.c.dispatch_state=='legacy_finished',legacy=='succeeded'),'completed'),
            (sa.and_(j.c.dispatch_state=='legacy_finished',legacy=='failed'),'failed'),
            (sa.and_(j.c.dispatch_state=='legacy_finished',legacy.in_(('cancelled','interrupted'))),'stopped'),
            (j.c.dispatch_state=='legacy_finished','attention'),
            (sa.and_(t.c.finished_at.is_not(None),t.c.status=='succeeded'),'completed'),
            (sa.and_(t.c.finished_at.is_not(None),t.c.status=='failed'),'failed'),
            (sa.and_(t.c.finished_at.is_not(None),t.c.status=='cancelled'),'stopped'),
            (sa.or_(t.c.status=='needs_attention',t.c.finished_at.is_not(None)),'attention'),
            (sa.or_(attention,j.c.payload['migration_pending'].as_boolean()==True),'attention'),
            (j.c.dispatch_state.in_(('waiting','paused')),'waiting'),
            else_='running')

    def history_page(self, key, page=1, status='all'):
        """同一查询条件先计数再分页；参数：资源、正整数页码、状态；返回：记录及分页信息。"""
        if type(page) is not int or page<1 or status not in HISTORY_STATUSES:
            raise ValueError('采集任务页码或状态无效')
        j=self.tables['collection_jobs']
        bucket=self.history_status()
        query=self._statement().add_columns(bucket.label('history_status')).where(j.c.execution_key==key)
        if status!='all':
            query=query.where(bucket==status)
        with self.connection() as c, c.begin():
            total=c.scalar(sa.select(sa.func.count()).select_from(query.subquery()))
            pages=max(1,(total+19)//20)
            page=min(page,pages)
            rows=c.execute(query.order_by(j.c.created_at.desc(),j.c.id.desc()).offset((page-1)*20).limit(20)).mappings().all()
        return {'jobs':[self._public_row(row) for row in rows],'page':page,'page_size':20,
            'total':total,'pages':pages,'status':status}

    def pause(self, key, paused=True, reason='用户停止或执行中断'):
        """原子修改资源和等待申请；参数：资源、暂停开关、原因；返回：无。"""
        q,j = self.tables['collection_queue_states'],self.tables['collection_jobs']
        with self.connection(True) as c:
            queue = self.resource(c,key)
            c.execute(q.update().where(q.c.execution_key==key).values(paused=paused,reason=reason,revision=queue['revision']+1,updated_at=database_now()))
            c.execute(j.update().where(j.c.execution_key==key,j.c.dispatch_state==('waiting' if paused else 'paused')).values(
                dispatch_state='paused' if paused else 'waiting',message=reason,revision=j.c.revision+1,updated_at=database_now()))

    def claim(self, key, job_id=None, *, manual=False):
        """资源行锁下领取一项；参数：资源、可选指定申请、明确单次执行；返回：申请或空。"""
        j,t = self.tables['collection_jobs'],self.tables['tasks']
        with self.connection(True) as c:
            queue = self.resource(c,key)
            if queue['paused'] and not (manual and job_id):
                return None
            active = c.scalar(sa.select(j.c.id).outerjoin(t,j.c.task_id==t.c.id).where(j.c.execution_key==key,
                sa.or_(j.c.dispatch_state=='claimed',sa.and_(j.c.dispatch_state=='started',t.c.finished_at.is_(None)))).limit(1))
            if active:
                return None
            query = sa.select(j).where(j.c.execution_key==key,j.c.dispatch_state.in_(('waiting','paused') if manual else ('waiting',)),j.c.stop_requested_at.is_(None))
            if job_id:
                query=query.where(j.c.id==job_id)
            row=c.execute(query.order_by(j.c.created_at,j.c.id).limit(1).with_for_update()).mappings().first()
            if row is None or row['payload'].get('migration_pending'):
                return None
            c.execute(j.update().where(j.c.id==row['id']).values(dispatch_state='claimed',claim_token=uuid4().hex,
                claimed_at=database_now(),message='后台正在启动',revision=row['revision']+1,updated_at=database_now()))
            selected=row['id']
        return self.read(selected)

    def bind_process(self, job_id, token, pid, started_at, *, replace_owner=False):
        """核对领取令牌后登记进程；参数：申请、令牌、进程归属；返回：是否绑定。"""
        j=self.tables['collection_jobs']
        with self.connection(True) as c:
            ownership=sa.true() if replace_owner else sa.or_(j.c.owner_pid.is_(None),j.c.owner_pid==pid)
            return bool(c.execute(j.update().where(j.c.id==job_id,j.c.claim_token==token,j.c.dispatch_state=='claimed',j.c.stop_requested_at.is_(None),ownership).values(
                owner_pid=pid,owner_process_started_at=database_time(started_at),revision=j.c.revision+1,updated_at=database_now())).rowcount)

    def runtime(self, row):
        """仅保存展示进度，不能覆盖实际执行状态；参数：内部描述；返回：无。"""
        j=self.tables['collection_jobs']
        with self.connection(True) as c:
            old=c.execute(sa.select(j).where(j.c.id==row['id']).with_for_update()).mappings().one()
            if old['claim_token']!=row.get('claim_token'):
                raise JobConflict('采集申请领取已变化，旧进程不能写入')
            payload=deepcopy(old['payload'])
            for name in ('progress','recovery_ready'):
                if name in row:
                    payload[name]=row[name]
            c.execute(j.update().where(j.c.id==row['id']).values(payload=payload,revision=old['revision']+1,updated_at=database_now()))

    def fail_start(self, job_id, token, message, *, interrupted=False):
        """只结束尚未创建实际执行的申请；参数：申请、令牌、原因；返回：无。"""
        j=self.tables['collection_jobs']
        with self.connection(True) as c:
            c.execute(j.update().where(j.c.id==job_id,j.c.claim_token==token,j.c.dispatch_state=='claimed',j.c.task_id.is_(None)).values(
                dispatch_state='interrupted' if interrupted else 'start_failed',request_key=None,message=message,
                revision=j.c.revision+1,updated_at=database_now()))

    def prompt(self, job_id, token, prompt):
        """保存当前提示与存储暂停；参数：申请、令牌、提示或空；返回：无。"""
        j=self.tables['collection_jobs']
        with self.connection(True) as c:
            changed=c.execute(j.update().where(j.c.id==job_id,j.c.claim_token==token,j.c.dispatch_state=='started').values(
                prompt=prompt,revision=j.c.revision+1,updated_at=database_now())).rowcount
            if not changed:
                raise JobConflict('提示所属执行已变化')

    def control(self, job_id, action, prompt_id=None, *, command_id=None):
        """核对目标与提示后保存指令；参数：申请、动作、提示及幂等编号；返回：指令编号。"""
        if action not in {'stop','pause','continue','confirm','cancel'}:
            raise ValueError('不支持的采集控制指令')
        if action in {'stop','pause'}:
            prompt_id=None
        j,t,k=(self.tables[name] for name in ('collection_jobs','tasks','collection_job_controls'))
        command_id=identifier(command_id or uuid4().hex)
        with self.connection(True) as c:
            old=c.execute(sa.select(j).where(j.c.id==job_id).with_for_update()).mappings().one()
            existing=c.execute(sa.select(k).where(k.c.id==command_id)).mappings().first()
            if existing:
                if existing['job_id']!=job_id or existing['action']!=action or existing['prompt_id']!=prompt_id:
                    raise JobConflict('控制编号与原指令不一致')
                return command_id
            finished=c.scalar(sa.select(t.c.finished_at).where(t.c.id==old['task_id'])) if old['task_id'] else None
            if old['dispatch_state'] in TERMINAL or finished is not None:
                raise JobConflict('任务已经结束，请刷新页面')
            if action!='stop' and old['stop_requested_at'] is not None:
                raise JobConflict('任务已请求停止，不能继续或确认')
            if action in {'continue','confirm','cancel'}:
                prompt=old['prompt'] or {}
                reply={'continue':'继续','confirm':'确认','cancel':'取消'}[action]
                if not prompt_id or prompt.get('prompt_id')!=prompt_id or reply not in prompt.get('allowed',[]) or prompt.get('storage_pending') or prompt.get('halted'):
                    raise JobConflict('提示已更新或当前正在等待入库，不能继续或确认')
                if c.scalar(sa.select(k.c.id).where(k.c.job_id==job_id,k.c.state=='pending')):
                    raise JobConflict('上一条指令尚未接收，请稍候')
            values={'revision':old['revision']+1,'updated_at':database_now()}
            if action=='stop':
                values['stop_requested_at']=database_now()
                if old['task_id'] is None:
                    values.update(dispatch_state='cancelled',request_key=None,message='已取消尚未执行的采集申请')
            c.execute(j.update().where(j.c.id==job_id).values(**values))
            c.execute(k.insert().values(id=command_id,job_id=job_id,claim_token=old['claim_token'],prompt_id=prompt_id,
                action=action,state='consumed' if action=='stop' and old['task_id'] is None else 'pending',
                created_at=database_now(),handled_at=database_now() if action=='stop' and old['task_id'] is None else None))
        return command_id

    def receive_control(self, job_id, token, *, stop_only=False):
        """领取本次执行指令，停止优先且持续有效；参数：申请、令牌；返回：指令或空。"""
        j,k=self.tables['collection_jobs'],self.tables['collection_job_controls']
        with self.connection(True) as c:
            old=c.execute(sa.select(j).where(j.c.id==job_id).with_for_update()).mappings().one()
            if old['claim_token']!=token:
                raise JobConflict('当前执行领取已失效，必须停止')
            if old['stop_requested_at'] is not None:
                c.execute(k.update().where(k.c.job_id==job_id,k.c.state=='pending',k.c.action=='stop').values(state='consumed',handled_at=database_now()))
                return {'action':'stop'}
            if stop_only:
                return None
            rows=c.execute(sa.select(k).where(k.c.job_id==job_id,k.c.state=='pending').order_by(k.c.created_at,k.c.id).with_for_update()).mappings().all()
            for row in rows:
                valid=row['claim_token']==token
                c.execute(k.update().where(k.c.id==row['id']).values(state='consumed' if valid else 'rejected',handled_at=database_now()))
                if valid:
                    return dict(row)
        return None
