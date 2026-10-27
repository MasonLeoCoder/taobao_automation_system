"""任务本地持久流水、有界唤醒队列和幂等补写；不抓取终端输入或重放浏览器动作。"""
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime,timezone
from ecommerce.infrastructure.storage.time_values import BEIJING
import json
import logging
import os
import queue
import re
import threading
import time
from uuid import uuid4
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode

from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.infrastructure.tasks.ownership import atomic_json,execution_key,process_started_at
from ecommerce.shared.exceptions import NeedsAttention,TaskBusy,TaskStopped

_CURRENT=ContextVar('task_event_session',default=None)
_THREAD_CURRENT=threading.local()


def sanitize(value):
    """脱敏日志值；参数：文本或结构化值；返回：去除凭据与签名参数的副本。"""
    sensitive=re.compile(r'password|passwd|cookie|authorization|access_token|refresh_token|secret',re.I)
    if isinstance(value,dict):
        return {str(k):'[已隐藏]' if sensitive.search(str(k)) else sanitize(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):
        return [sanitize(v) for v in value]
    if not isinstance(value,str):
        return value
    def clean_url(match):
        """只保留日志必要的页面路由；参数：匹配对象；返回：安全 URL。"""
        try:
            url=urlsplit(match.group())
            return urlunsplit((url.scheme,url.hostname or '',url.path,urlencode([(k,v) for k,v in parse_qsl(url.query) if k in {'id','itemId','item_id','skuId'}]),''))
        except ValueError:
            return '[链接已隐藏]'
    value=re.sub(r'https?://[^\s<>"\']+',clean_url,value)
    value=re.sub(r'(?im)\b(cookie|authorization)\s*[:=][^\r\n]+',r'\1=[已隐藏]',value)
    return re.sub(r'(?i)\b(password|passwd|cookie|authorization|access_token|refresh_token|secret)\b[\s"\']*[:=][\s"\']*[^\s,;"\']+',r'\1=[已隐藏]',value)


class EventJournal:
    """每个任务或进程独占一个追加文件，确认位置仅在数据库提交后推进。"""
    def __init__(self,settings,key):
        if not re.fullmatch('[a-f0-9]{32}',key):
            raise ValueError('事件流水编号格式无效')
        self.settings=settings
        self.key=key
        self.path=settings.log_dir/'events'/f'{key}.jsonl'
        self.ack=self.path.with_suffix('.ack.json')
        self.lock=threading.Lock()

    def append(self,operation):
        """先落盘再尝试入库；参数：事件操作；返回：无，磁盘失败阻止后续业务。"""
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with self.lock,self.path.open('ab') as handle:
                handle.write((json.dumps(operation,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8'))
                handle.flush()
                os.fsync(handle.fileno())
                return handle.tell()
        except (OSError,TypeError,ValueError) as error:
            raise NeedsAttention('本地事件流水无法安全保存，已停止后续业务动作') from error

    def pending(self):
        """查询是否存在未确认内容；参数：无；返回：布尔值。"""
        offset=self._offset()
        size=self.path.stat().st_size if self.path.exists() else 0
        if offset<0 or offset>size:
            raise NeedsAttention('事件流水确认位置异常，请核对文件，未静默重置')
        return size>offset

    def _offset(self):
        """校验确认位置；短暂读取占用最多尝试3次，损坏时保留现场。"""
        try:
            for attempt in range(3):
                try:
                    with self.lock:
                        raw=self.ack.read_text(encoding='utf-8') if self.ack.exists() else None
                    break
                except PermissionError:
                    if attempt==2:
                        raise
                    time.sleep(.02*(attempt+1))
            offset=json.loads(raw)['offset'] if raw is not None else 0
            if type(offset) is not int or offset<0:
                raise ValueError('确认位置格式无效')
            return offset
        except (OSError,ValueError,KeyError,TypeError) as error:
            raise NeedsAttention('本地事件确认文件无法核对，请保留现场；未重置或跳过记录') from error

    def drain(self,store):
        """跨进程互斥补写；参数：任务存储；返回：提交记录数量。"""
        count=0
        with shop_lock(self.settings.workspace/'storage/event_locks'/self.key):
            offset=self._offset()
            if not self.path.exists():
                return 0
            if offset>self.path.stat().st_size:
                raise NeedsAttention('事件流水比已确认位置短，请核对文件')
            with self.path.open('rb') as handle:
                handle.seek(offset)
                while True:
                    with self.lock:
                        line=handle.readline()
                    if not line:
                        break
                    if not line.endswith(b'\n'):
                        raise NeedsAttention('事件流水末条未完整保存，请保留现场后核对')
                    try:
                        operation=json.loads(line)
                    except (ValueError,UnicodeDecodeError) as error:
                        raise NeedsAttention('本地事件流水格式损坏，请保留现场；未跳过记录') from error
                    if operation.get('database')!=store.settings.database:
                        raise MySQLStorageError('本地流水属于另一数据库，未跨库补写')
                    if operation.get('op')=='collection':
                        from ecommerce.infrastructure.storage.collection_store import collection_store
                        collection_store(self.settings).apply_operation(operation)
                    else:
                        store.apply_operation(operation)
                    with self.lock:
                        atomic_json(self.ack,{'offset':handle.tell(),'event_key':operation['event_key'],'database':store.settings.database})
                    count+=1
        return count


def pending_task_status(settings, task_id):
    """只读未入库流水末尾的阻断提示；参数：配置、已核实的任务；返回：提示或空，不推进确认。"""
    journal=EventJournal(settings,task_id)
    if not journal.path.exists():
        return None
    try:
        offset=journal._offset()
        with journal.path.open('rb') as handle:
            size=handle.seek(0,2)
            if offset>size:
                raise NeedsAttention('事件流水确认位置异常，请保留现场核对')
            start=max(offset,size-131072)
            handle.seek(start)
            if start>offset:
                handle.readline(131072)  # 截断的首条不作为状态证据。
            lines=handle.read(131072).splitlines(keepends=True)
        result=None
        for line in lines:
            if not line.endswith(b'\n'):
                continue
            try:
                operation=json.loads(line)
            except (ValueError,UnicodeDecodeError):
                continue
            if (not isinstance(operation,dict) or operation.get('task_id')!=task_id
                    or operation.get('database')!=settings.mysql.database):
                continue
            event=operation.get('event_type')
            details=operation.get('details') or {}
            if event=='session_halted' and operation.get('state')=='needs_attention':
                reason=details.get('reason') if isinstance(details,dict) else None
                result={'state':'halted','message':sanitize(reason if isinstance(reason,str) and reason else '业务已中断，浏览器仅保留现场'),
                        'step':operation.get('step'),'time':operation.get('time'),'source':'local_pending'}
            elif event=='storage_wait':
                result={'state':'storage_pending','message':sanitize(operation.get('message') or '采集结果尚未确认入库'),
                        'step':operation.get('step'),'time':operation.get('time'),'source':'local_pending'}
            elif event=='task_finished':
                result={'state':'finished_pending','message':sanitize(operation.get('message') or '任务已结束，结果尚未确认入库'),
                        'step':operation.get('step'),'time':operation.get('time'),'source':'local_pending'}
            elif event in {'storage_resumed','session_ended'} or (
                    event=='state_changed' and operation.get('state') in {'running','resuming','stopped'}):
                result=None
        return result
    except (OSError,NeedsAttention,ValueError) as error:
        return {'state':'unverified','message':'无法核对本地任务提示：'+sanitize(str(error)),
                'source':'local_pending','step':None,'time':None}


class TaskEvents:
    """有界队列只负责唤醒，全部积压记录留在磁盘，内存不随日志量增长。"""
    def __init__(self,settings,store,name,task_id=None):
        self.settings,self.store,self.name=settings,store,name
        self.task_id=task_id or uuid4().hex
        self.journal=EventJournal(settings,self.task_id)
        self.signal=queue.Queue(maxsize=1)
        self.stopping=threading.Event()
        self.error=None
        self.closing=False
        self.waiting=False
        self.worker=None
        self.collection_barrier=0
        self.recovery_exported_revision=0
        self.active_path=settings.workspace/'storage/task_controls'/f'{self.task_id}.active.json'
        self.owner={'id':self.task_id,'name':name,'execution_key':execution_key(settings),'owner_pid':os.getpid(),
                    'owner_process_started_at':process_started_at(os.getpid()),'database':store.settings.database}

    def operation(self,op,event_type,message=None,*,state=None,level='INFO',step=None,details=None):
        """生成稳定操作记录；参数：操作及上下文；返回：可序列化记录。"""
        return {'op':op,'database':self.store.settings.database,'task_id':self.task_id,'event_key':uuid4().hex,'time':datetime.now(BEIJING).isoformat(),
                'event_type':event_type,'message':sanitize(message),'state':state,'level':level,'step':sanitize(step),'details':sanitize(details or {})}

    def start(self):
        """先持久保存创建记录，再确认数据库后启动业务；参数：无；返回：无。"""
        operation=self.operation('create','task_started','任务开始：'+self.name,state='running')
        if self.name.startswith('collection.taobao.') and self.settings.collection_backend=='mysql':
            from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
            try:
                row=CollectionJobStore(self.settings).read(self.task_id)
                if getattr(self,'expected_job_claim',None) and self.expected_job_claim!=row['claim_token']:
                    raise NeedsAttention('采集领取已失效，旧进程未启动浏览器')
                if row['dispatch_state']!='claimed' or row.get('stop_requested'):
                    raise TaskStopped('采集申请已取消或启动已中断，未打开浏览器')
                self.job_claim_token=row['claim_token']
                self.job_origin=row.get('origin')
                self.job_modules=row.get('modules')
                operation['job_claim_token']=self.job_claim_token
            except LookupError:
                pass
        operation['task']={k:v for k,v in self.owner.items() if k not in {'database'}}
        operation['task'].update(status='running',started_at=operation['time'],origin='live')
        self.journal.append(operation)
        atomic_json(self.active_path,self.owner)
        self.journal.drain(self.store)
        if getattr(self,'job_claim_token',None):
            row=CollectionJobStore(self.settings).read(self.task_id)
            if row.get('task_id')!=self.task_id or row.get('claim_token')!=self.job_claim_token:
                raise TaskStopped('创建入库前申请已取消，未打开浏览器')
            self.started=True
            if row.get('stop_requested'):
                raise TaskStopped('采集开始前收到停止请求，未打开浏览器')
        self.worker=threading.Thread(target=self._work,daemon=True,name='mysql-events-'+self.task_id[:8])
        self.worker.start()

    def _work(self):
        """仅执行幂等数据库写入；参数：无；返回：无，不调用浏览器对象。"""
        while not self.stopping.is_set():
            try:
                self.signal.get(timeout=.5)
            except queue.Empty:
                pass
            try:
                self.journal.drain(self.store)
                self.error=None
            except TaskBusy:
                continue
            except (MySQLStorageError,OSError,ValueError,NeedsAttention) as error:
                self.error=error
                self.stopping.wait(.5)
            except Exception as error:
                self.error=NeedsAttention('持久化处理遇到不可重试问题：'+type(error).__name__+'；流水已保留，请核对日志')
                logging.getLogger(__name__).exception('持久化线程已停止后续处理')
                self.stopping.set()

    def append(self,operation):
        """追加并唤醒写入线程；参数：持久操作；返回：无。"""
        offset=self.journal.append(operation)
        try:
            self.signal.put_nowait(True)
        except queue.Full:
            pass
        return offset

    def collection(self,snapshot,context):
        """排入不可变采集快照；参数：引用、发布依据；返回：无，下个页面屏障前确认。"""
        operation=self.operation('collection','collection_saved','采集结果等待入库')
        recovery=getattr(self,'recovery',None)
        if recovery and recovery.revision>self.recovery_exported_revision:
            context={**context,'recovery':recovery.snapshot()}
        operation.update(snapshot=snapshot,context=context)
        self.collection_barrier=self.append(operation)
        if recovery:
            self.recovery_exported_revision=recovery.revision

    def emit(self,event_type,message=None,*,level='INFO',step=None,state=None,details=None,logger_name=None):
        """记录普通事件；参数：提示与上下文；返回：无。"""
        if not self.closing:
            operation=self.operation('event',event_type,message,level=level,step=step,state=state,details=details)
            operation['logger_name']=logger_name
            self.append(operation)

    def progress(self,status,message):
        """持久保存任务中途状态；参数：状态与说明；返回：无。"""
        if status not in {'running','needs_attention'}:
            raise ValueError('执行中状态不受支持')
        self.append(self.operation('progress','task_progress',message,state=status))

    def checkpoint(self,step=None,pump=None):
        """断库时在安全边界等待，停止命令始终可用；参数：步骤、浏览器事件泵；返回：无。"""
        barrier_pending=self.collection_barrier>self.journal._offset()
        if isinstance(self.error,(NeedsAttention,ValueError)) or getattr(self.error,'retryable',True) is False:
            raise NeedsAttention(str(self.error))
        if (self.error is None and not barrier_pending) or self.waiting:
            return
        reported=self.error is not None
        if reported:
            self.emit('storage_wait','数据库写入暂不可用，结果已保留本地；等待恢复或停止任务',level='WARNING',step=step,state='needs_attention')
            print('数据库写入暂不可用，任务在当前检查点暂停；已保留本地流水，可用 taobao stop 停止。',flush=True)
        control=self.settings.workspace/'storage/task_controls'/f'{self.task_id}.json'
        self.waiting=True
        atomic_json(self.active_path,dict(self.owner,storage_pending=True))
        try:
            while self.error is not None or self.journal.pending():
                if getattr(self,'job_claim_token',None):
                    from ecommerce.infrastructure.tasks.collection_control import receive
                    _,request=receive(stop_only=True)
                    if request and request.get('action')=='stop':
                        raise TaskStopped('用户已停止任务；已采结果和待入库记录保留')
                if isinstance(self.error,(NeedsAttention,ValueError)) or getattr(self.error,'retryable',True) is False:
                    raise NeedsAttention(str(self.error))
                if self.error is not None and not reported:
                    self.emit('storage_wait','采集数据尚未入库，已在检查点暂停并保留本地快照',level='WARNING',step=step,state='needs_attention')
                    print('采集数据尚未入库，任务已暂停；可等待恢复或使用 taobao stop 停止。',flush=True)
                    reported=True
                if control.exists():
                    request=json.loads(control.read_text(encoding='utf-8'))
                    if request.get('action')=='stop':
                        control.unlink()
                        raise TaskStopped('用户已停止任务；已采结果和待入库记录保留本地')
                if pump:
                    pump()
                self.stopping.wait(.1)
        finally:
            self.waiting=False
            atomic_json(self.active_path,self.owner)
        if reported:
            self.emit('storage_resumed','数据库连接及待入库记录已恢复',step=step,state='running')

    def finish(self,status,message):
        """持久保存真实结论并等待提交；参数：结果；返回：数据库是否已确认。"""
        self.closing=True
        self.append(self.operation('finish','task_finished',message,state=status,level='ERROR' if status=='failed' else 'INFO'))
        deadline=time.monotonic()+max(3,self.store.settings.connect_timeout+1)
        while self.journal.pending() and time.monotonic()<deadline:
            if self.error:
                break
            self.stopping.wait(.05)
        return not self.journal.pending()

    def close(self):
        """停止后台线程并清理活动标记；参数：无；返回：无，保留全部流水。"""
        self.stopping.set()
        if self.worker:
            self.worker.join(timeout=2)
        self.active_path.unlink(missing_ok=True)


def current_events():
    """取得任务会话；参数：无；返回：当前上下文或同线程浏览器回调的会话。"""
    return _CURRENT.get() or getattr(_THREAD_CURRENT,'session',None)


@contextmanager
def event_session(session):
    """绑定任务及所属线程；参数：会话；返回：作用域，退出后恢复两种绑定。"""
    # Playwright 同步事件另建 greenlet，不继承 ContextVar；同一执行线程仍属于当前任务。
    previous=getattr(_THREAD_CURRENT,'session',None)
    _THREAD_CURRENT.session=session
    token=_CURRENT.set(session)
    try:
        yield session
    finally:
        _CURRENT.reset(token)
        _THREAD_CURRENT.session=previous


def emit_event(event_type,message=None,**kwargs):
    """向当前任务记录结构化事件；参数：类型、提示、上下文；返回：无。"""
    session=current_events()
    if session:
        session.emit(event_type,message or kwargs.get('step') or event_type,**kwargs)


def storage_checkpoint(step=None,pump=None):
    """为平台检查点提供存储暂停；参数：步骤、事件泵；返回：无。"""
    session=current_events()
    if session:
        session.checkpoint(step,pump)


def notify(*values,sep=' ',end='\n',flush=True,**kwargs):
    """保留命令行输出并同步文件/数据库；参数：原输出内容；返回：无。"""
    message=sanitize(sep.join(str(v) for v in values))
    print(message,end=end,flush=flush,**kwargs)
    if message.strip('\a\r\n \t'):
        logging.getLogger('ecommerce.progress').info(message,extra={'suppress_console':True,'event_type':'progress'})


class EventLogHandler(logging.Handler):
    """将现有 logging 接到当前任务流水，避免数据库错误递归记录。"""
    def __init__(self,settings):
        super().__init__()
        self.settings=settings
        self.unscoped=EventJournal(settings,uuid4().hex)
        self.stop=threading.Event()
        self.wake=threading.Event()
        self.thread=None

    def _work(self):
        """后台补写不属于任务的命令日志；参数：无；返回：无，故障保留流水。"""
        while not self.stop.is_set():
            self.wake.wait(.5)
            self.wake.clear()
            try:
                self.unscoped.drain(self.settings.task_database)
            except (MySQLStorageError,OSError,ValueError,NeedsAttention,TaskBusy):
                self.stop.wait(.5)

    def emit(self,record):
        if record.name.startswith('ecommerce.infrastructure.storage.'):
            return
        session=current_events()
        message=record.getMessage()
        if record.exc_text:
            message+='\n'+record.exc_text
        if session:
            session.emit(getattr(record,'event_type','log'),message,level=record.levelname,logger_name=record.name)
        else:
            self.unscoped.append({'op':'event','database':self.settings.mysql.database,'task_id':None,'event_key':uuid4().hex,
                'time':datetime.now(BEIJING).isoformat(),'event_type':'log','level':record.levelname,
                'logger_name':record.name,'message':sanitize(message)})
            if self.thread is None:
                self.thread=threading.Thread(target=self._work,daemon=True,name='mysql-general-events')
                self.thread.start()
            self.wake.set()

    def close(self):
        """释放后台资源，待补写记录留在磁盘；参数：无；返回：无。"""
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)
        super().close()


def replay_events(settings,store,*,include_active=False):
    """按流水幂等补写；参数：配置、存储、是否包含活跃任务；返回：提交总量。"""
    from ecommerce.infrastructure.tasks.ownership import active_local_tasks
    active={r['id'] for r in active_local_tasks(settings)}
    count=0
    for path in sorted((settings.log_dir/'events').glob('*.jsonl')):
        if not include_active and path.stem in active:
            continue
        journal=EventJournal(settings,path.stem)
        with path.open('r',encoding='utf-8') as handle:
            first=handle.readline()
        try:
            if first and json.loads(first).get('database')!=store.settings.database:
                continue
            if journal.ack.exists():
                ack=json.loads(journal.ack.read_text(encoding='utf-8'))
                if ack.get('database')!=store.settings.database:
                    continue
        except (ValueError,TypeError) as error:
            raise NeedsAttention('本地补写流水或确认文件格式异常，请保留现场核对') from error
        try:
            count+=journal.drain(store)
        except TaskBusy:
            continue
    return count
