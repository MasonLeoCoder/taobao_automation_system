"""任务与日志历史导入：只读快照、内容核对和整批发布，不恢复浏览器执行。"""
from collections import Counter
from contextlib import contextmanager,closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage.mysql import MySQLStorageError,safe_error
from ecommerce.infrastructure.storage.task_store import fingerprint,database_time
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.tasks.events import sanitize
from ecommerce.infrastructure.tasks.lock import shop_lock

PARSER_VERSION='task_history_v1'
# 来源身份命名空间固定，解析器升级不能改变既有记录身份。
IDENTITY_NAMESPACE='task_history_v1'
HEADER=re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\] ([^\r\n]+?) - (.*)$')
STATUSES={'running','succeeded','failed','cancelled','needs_attention'}


def instant():
    """生成北京时间数据库时间；参数：无；返回：无时区 datetime。"""
    return database_now()


def source_hash(path):
    """计算快照校验和；参数：文件；返回：SHA-256。"""
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def _optional_lock(settings,enabled):
    """复用执行资源锁；参数：配置、是否自行加锁；返回：上下文。"""
    if enabled:
        with shop_lock(settings.profile_path):
            yield
    else:
        yield


def snapshot_history(settings,*,take_lock=True):
    """备份本阶段来源；参数：配置及是否已持资源锁；返回：不可变快照清单路径。"""
    if settings.task_backend!='sqlite':
        raise MySQLStorageError('任务源已切换；请使用切换前的快照清单复核，不能再次全量导入实时日志')
    with _optional_lock(settings,take_lock):
        destination=settings.workspace/'backups'/('task_history_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8])
        destination.mkdir(parents=True,exist_ok=False)
        sources=[]
        if settings.database.exists():
            path=destination/'tasks.sqlite3'
            with closing(sqlite3.connect(settings.database.as_uri()+'?mode=ro',uri=True)) as original,closing(sqlite3.connect(path)) as backup:
                original.backup(backup)
            sources.append(('task_sqlite',settings.database,path))
        for index,original in enumerate(sorted((settings.log_dir/'taobao').glob('*.jsonl'))):
            path=destination/f'events_{index:04d}.jsonl'
            path.write_bytes(original.read_bytes())
            sources.append(('taobao_events',original,path))
        original=settings.log_dir/'run.log'
        if original.exists():
            path=destination/'run.log'
            # 捕获当时已落盘的字节；此后追加的实时记录不进入该快照。
            with original.open('rb') as handle:
                content=handle.read(original.stat().st_size)
            path.write_bytes(content)
            sources.append(('text_log',original,path))
        manifest={'parser_version':PARSER_VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
            'scope':'tasks_and_logs_only','sources':[
                {'kind':kind,'relative_path':original.relative_to(settings.workspace).as_posix(),'snapshot':path.name,
                 'size_bytes':path.stat().st_size,'sha256':source_hash(path)} for kind,original,path in sources]}
        target=destination/'manifest.json'
        with target.open('x',encoding='utf-8') as handle:
            json.dump(manifest,handle,ensure_ascii=False,indent=2)
        return target


def _text(value,maximum,label):
    """拒绝超出结构的输入；参数：值、字符或字节上限、字段名；返回：原值。"""
    if value is not None and (not isinstance(value,str) or len(value)>maximum or (maximum==65535 and len(value.encode('utf-8'))>maximum)):
        raise MySQLStorageError('历史记录超过字段容量或格式错误：'+label+'；原件及快照已保留，未截断导入')
    return value


def _source_records(path,source):
    """解析一个快照；参数：快照路径和来源；返回：目标表、稳定记录键、值及原文指纹。"""
    kind=source['kind']
    if kind=='task_sqlite':
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as connection:
            connection.row_factory=sqlite3.Row
            for row in connection.execute('SELECT id,name,status,started_at,finished_at,message FROM tasks ORDER BY id'):
                raw=dict(row)
                if not re.fullmatch('[a-f0-9]{32}',raw['id']) or raw['status'] not in STATUSES:
                    raise MySQLStorageError('历史任务编号或状态无效')
                if raw['status']=='running' or (raw['status']=='needs_attention' and raw['finished_at'] is None):
                    raise MySQLStorageError('旧任务库仍有未结束任务；请先核对或结束原任务再切换')
                values={**raw,'started_at':database_time(raw['started_at']),'finished_at':database_time(raw['finished_at']),
                        'message':sanitize(raw['message']),'origin':'historical'}
                _text(values['name'],96,'tasks.name')
                _text(values['message'],65535,'tasks.message')
                yield 'tasks',raw['id'],values,fingerprint(raw)
        return
    if kind=='taobao_events':
        with path.open('rb') as handle:
            while True:
                offset=handle.tell()
                line=handle.readline()
                if not line:
                    break
                if not line.endswith(b'\n'):
                    raise MySQLStorageError('结构化历史日志末条不完整，未忽略源记录')
                if not line.strip():
                    continue
                raw=json.loads(line)
                value=sanitize(raw)
                task_id=value.get('task_id')
                if task_id and not re.fullmatch('[a-f0-9]{32}',task_id):
                    raise MySQLStorageError('历史事件任务编号格式无效')
                at=raw.get('time')
                values={'task_id':task_id,'event_type':value.get('event','log'),'state':value.get('state'),
                        'step':value.get('step'),'message':value.get('message') or value.get('step') or value.get('event'),
                        'level':value.get('level'),'occurred_at':database_time(at),'time_raw':at,
                        'time_basis':'explicit_offset' if at else 'unknown','details':{'source_record':value}}
                for key,maximum in [('event_type',64),('state',32),('step',255),('level',16),('time_raw',128),('message',65535)]:
                    _text(values.get(key),maximum,'task_events.'+key)
                yield 'task_events',str(offset),values,fingerprint(raw)
        return
    if kind!='text_log':
        raise MySQLStorageError('快照来源类型不受支持')
    with path.open('rb') as handle:
        current=None
        while True:
            offset=handle.tell()
            line=handle.readline()
            if not line:
                break
            text=line.decode('utf-8').rstrip('\r\n')
            match=HEADER.match(text)
            if match:
                if current:
                    yield _log_record(*current)
                current=(str(offset),match.group(1),match.group(2),match.group(3),[match.group(4)])
            elif current:
                current[4].append(text)
            elif text:
                current=(str(offset),None,None,None,[text])
        if current:
            yield _log_record(*current)


def _log_record(key,at,level,logger,lines):
    """组合多行异常；参数：字节位置、原时间、级别、模块和正文；返回：待导入记录。"""
    raw={'time':at,'level':level,'logger':logger,'message':'\n'.join(lines)}
    _text(raw['message'],65535,'task_events.message')
    _text(logger,255,'task_events.logger_name')
    return 'task_events',key,{'task_id':None,'event_type':'log','message':sanitize(raw['message']),
        'level':level,'logger_name':logger,'occurred_at':None,'time_raw':at,'time_basis':'unknown',
        'details':{'time_zone_unknown':True,'task_link_unknown':True}},fingerprint(raw)


def read_snapshot(manifest_path):
    """校验并解析固定快照；参数：清单绝对路径；返回：清单及全部已校验记录。"""
    manifest_path=Path(manifest_path).resolve()
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('parser_version')!=PARSER_VERSION or manifest.get('scope')!='tasks_and_logs_only':
        raise MySQLStorageError('快照解析版本或范围不匹配')
    parsed=[]
    identities=set()
    for source in manifest['sources']:
        path=(manifest_path.parent/source['snapshot']).resolve()
        if path.parent!=manifest_path.parent or path.stat().st_size!=source['size_bytes'] or source_hash(path)!=source['sha256']:
            raise MySQLStorageError('快照路径、大小或校验和不一致，未执行导入')
        relative=Path(source['relative_path'])
        identity=(source['kind'],relative.as_posix().casefold())
        if relative.is_absolute() or '..' in relative.parts or identity in identities:
            raise MySQLStorageError('来源身份重复或相对路径无效')
        identities.add(identity)
        parsed.append((source,list(_source_records(path,source))))
    # 无论清单顺序如何，先导任务，再导事件。
    parsed.sort(key=lambda item:(item[0]['kind']!='task_sqlite',item[0]['relative_path']))
    return manifest,parsed


def preview_history(manifest_path):
    """只读预览；参数：固定快照；返回：记录数量、原状态及限制，不写 MySQL。"""
    manifest,parsed=read_snapshot(manifest_path)
    tables=Counter()
    statuses=Counter()
    unknown_time=0
    for source,records in parsed:
        tables.update(row[0] for row in records)
        for table,key,values,digest in records:
            if table=='tasks':
                statuses[values['status']]+=1
            elif values['occurred_at'] is None:
                unknown_time+=1
    return {'parser_version':PARSER_VERSION,'manifest':str(Path(manifest_path).resolve()),
        'manifest_sha256':fingerprint(manifest['sources']),'sources':len(parsed),'records':dict(tables),
        'task_statuses':dict(statuses),'unknown_event_time':unknown_time,'preview':True,
        'limits':['仅导入任务、淘宝结构化事件和 run.log；原件保留','文本日志不猜测时区或任务归属','历史任务不执行恢复或浏览器动作']}


def import_history(store,manifest_path):
    """核对后整批发布；参数：目标任务存储、固定快照；返回：可审计结果，重复输入幂等。"""
    preview=preview_history(manifest_path)
    manifest,parsed=read_snapshot(manifest_path)
    store.initialize()
    tables=store.tables
    runs,sources,mappings,issues=tables['import_runs'],tables['import_sources'],tables['import_row_mappings'],tables['import_issues']
    run_id=None
    lock_name='task_history_'+store.settings.database
    try:
        with store.engine.connect() as connection:
            if connection.execute(sa.text('SELECT GET_LOCK(:name,0)'),{'name':lock_name}).scalar()!=1:
                raise MySQLStorageError('另一历史导入正在执行，请稍后重试')
            connection.commit()
            try:
                existing=connection.execute(sa.select(runs).where(runs.c.manifest_sha256==preview['manifest_sha256'],
                    runs.c.parser_version==PARSER_VERSION,runs.c.status=='committed')).mappings().first()
                connection.commit()
                if existing:
                    return {**preview,'preview':False,'database':store.settings.database,'import_run_id':existing['id'],
                            'status':'committed','already_committed':True,'counts':existing['counts']}
                with connection.begin():
                    run_id=connection.execute(runs.insert().values(started_at=instant(),status='running',
                        scope={'kind':'tasks_and_logs_only','manifest':str(Path(manifest_path).resolve())},
                        manifest_sha256=preview['manifest_sha256'],parser_version=PARSER_VERSION)).inserted_primary_key[0]
                counts={'tasks_inserted':0,'tasks_matched':0,'events_inserted':0,'events_matched':0,'issues':0,'conflicts':0}
                with connection.begin():
                    for source,records in parsed:
                        source_id=connection.execute(sources.insert().values(import_run_id=run_id,root_key='workspace',
                            relative_path=source['relative_path'],sha256=source['sha256'],size_bytes=source['size_bytes'],
                            source_kind=source['kind'],schema_version=_declared_version(Path(manifest_path).resolve().parent/source['snapshot'],source),
                            parse_status='parsed')).inserted_primary_key[0]
                        for table_name,key,original,digest in records:
                            values=dict(original)
                            logical=fingerprint([IDENTITY_NAMESPACE,source['kind'],source['relative_path'].casefold(),key,table_name])
                            old=connection.execute(sa.select(mappings).where(mappings.c.logical_key==logical)).mappings().all()
                            if any(row['payload_fingerprint']!=digest for row in old):
                                raise MySQLStorageError('同一历史来源记录内容冲突；本批已回滚，原件与已发布记录均保留')
                            target=tables[table_name]
                            if table_name=='tasks':
                                target_key=values['id']
                                present=connection.execute(sa.select(target).where(target.c.id==target_key).with_for_update()).mappings().one_or_none()
                                if present:
                                    compared=('id','name','status','started_at','finished_at','message','origin')
                                    if any(present[k]!=values[k] for k in compared):
                                        raise MySQLStorageError('历史任务编号冲突，未覆盖现有任务')
                                    if not _published_owner(connection,runs,present['import_run_id']):
                                        raise MySQLStorageError('同编号任务属于未发布导入，未复用或绕过隔离')
                                else:
                                    connection.execute(target.insert().values(**values,import_run_id=run_id))
                            else:
                                event_key='historical:'+logical
                                present=connection.execute(sa.select(target).where(target.c.event_key==event_key)).mappings().one_or_none()
                                if present:
                                    if (present['details'] or {}).get('source_fingerprint')!=digest:
                                        raise MySQLStorageError('历史事件内容冲突，未覆盖已导入事件')
                                    if not _published_owner(connection,runs,present['import_run_id']):
                                        raise MySQLStorageError('同键事件属于未发布导入，未复用或绕过隔离')
                                    target_key=str(present['id'])
                                else:
                                    task=None
                                    if values['task_id']:
                                        task=connection.execute(sa.select(tables['tasks']).where(tables['tasks'].c.id==values['task_id']).with_for_update()).mappings().one_or_none()
                                        if task is None:
                                            connection.execute(issues.insert().values(import_run_id=run_id,source_id=source_id,source_record_key=key,
                                                severity='warning',code='unknown_task',message='原事件任务不存在；保留原编号，未伪造任务关联',resolution='task_id 置空，原编号保留在 details.source_record'))
                                            counts['issues']+=1
                                            values['task_id']=None
                                    sequence=task['last_event_sequence']+1 if task else None
                                    details={**values.pop('details',{}),'source_fingerprint':digest,
                                             'source_kind':source['kind'],'relative_path':source['relative_path'],'record_key':key}
                                    result=connection.execute(target.insert().values(**values,task_sequence=sequence,event_key=event_key,
                                        details=details,origin='historical',recorded_at=instant(),import_run_id=run_id))
                                    target_key=str(result.inserted_primary_key[0])
                                    if task:
                                        connection.execute(tables['tasks'].update().where(tables['tasks'].c.id==task['id']).values(last_event_sequence=sequence))
                            result='matched' if present else 'inserted'
                            counts[('tasks_' if table_name=='tasks' else 'events_')+result]+=1
                            connection.execute(mappings.insert().values(source_id=source_id,source_record_key=key,
                                source_record_hash=fingerprint(key),logical_key=logical,target_table=table_name,target_key=target_key,
                                target_key_hash=fingerprint(target_key),payload_fingerprint=digest,result=result))
                    if counts['tasks_inserted']+counts['tasks_matched']!=preview['records'].get('tasks',0) or counts['events_inserted']+counts['events_matched']!=preview['records'].get('task_events',0):
                        raise MySQLStorageError('导入数量核对不一致，本批未发布')
                    connection.execute(runs.update().where(runs.c.id==run_id).values(status='committed',finished_at=instant(),counts=counts))
                return {**preview,'preview':False,'database':store.settings.database,'import_run_id':run_id,'status':'committed',
                        'already_committed':False,'counts':counts}
            except Exception:
                connection.rollback()
                if run_id is not None:
                    with connection.begin():
                        updated=connection.execute(runs.update().where(runs.c.id==run_id,runs.c.status!='committed').values(
                            status='failed',finished_at=instant(),counts={'rolled_back':True}))
                        if updated.rowcount:
                            connection.execute(issues.insert().values(import_run_id=run_id,severity='error',code='batch_rolled_back',
                                message='整批导入未通过；业务写入已回滚，快照保留供复核',resolution='按原快照重试；内容冲突须人工核对'))
                raise
            finally:
                connection.execute(sa.text('SELECT RELEASE_LOCK(:name)'),{'name':lock_name})
                connection.commit()
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def _published_owner(connection,runs,run_id):
    """复用前确认原导入已发布；参数：连接、执行表和归属；返回：布尔值。"""
    return run_id is not None and connection.scalar(sa.select(runs.c.status).where(runs.c.id==run_id))=='committed'


def _declared_version(path,source):
    """读取源明确声明的格式版本；参数：固定文件及类型；返回：版本或空，不用解析器版本代替。"""
    if source['kind']=='task_sqlite':
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as connection:
            return connection.execute('PRAGMA user_version').fetchone()[0] or None
    return None


def audit_history(store,manifest_path):
    """逐记录只读复核已发布内容；参数：任务存储、原快照；返回：字段和指纹核对数量。"""
    manifest,parsed=read_snapshot(manifest_path)
    tables=store.tables
    checked=Counter()
    try:
        with store.engine.connect() as connection:
            runs=connection.execute(sa.select(tables['import_runs']).where(
                tables['import_runs'].c.manifest_sha256==fingerprint(manifest['sources']),
                tables['import_runs'].c.parser_version==PARSER_VERSION,tables['import_runs'].c.status=='committed')).mappings().all()
            if len(runs)!=1:
                raise MySQLStorageError('没有唯一的已发布导入批次可供复核')
            run_id=runs[0]['id']
            actual_sources=connection.execute(sa.select(tables['import_sources']).where(tables['import_sources'].c.import_run_id==run_id)).mappings().all()
            source_by_key={(row['source_kind'],row['relative_path']):row for row in actual_sources}
            if len(source_by_key)!=len(parsed) or len(actual_sources)!=len(parsed):
                raise MySQLStorageError('导入来源文件数量不一致')
            for source,_ in parsed:
                actual=source_by_key.get((source['kind'],source['relative_path']))
                declared=_declared_version(Path(manifest_path).resolve().parent/source['snapshot'],source)
                if not actual or actual['sha256']!=source['sha256'] or actual['size_bytes']!=source['size_bytes'] or actual['schema_version']!=declared:
                    raise MySQLStorageError('来源文件哈希、大小或声明版本不一致')
            source_ids=sa.select(tables['import_sources'].c.id).where(tables['import_sources'].c.import_run_id==run_id)
            mappings=connection.execute(sa.select(tables['import_row_mappings']).where(
                tables['import_row_mappings'].c.source_id.in_(source_ids))).mappings().all()
            mapping_by_key={row['logical_key']:row for row in mappings}
            if len(mappings)!=sum(len(rows) for _,rows in parsed) or len(mapping_by_key)!=len(mappings):
                raise MySQLStorageError('来源映射数量或逻辑身份不一致')
            task_ids=[row['target_key'] for row in mappings if row['target_table']=='tasks']
            event_ids=[int(row['target_key']) for row in mappings if row['target_table']=='task_events']
            task_rows={row['id']:row for row in connection.execute(sa.select(tables['tasks']).where(tables['tasks'].c.id.in_(task_ids))).mappings()}
            event_rows={str(row['id']):row for row in connection.execute(sa.select(tables['task_events']).where(tables['task_events'].c.id.in_(event_ids))).mappings()}
            committed_ids=set(connection.execute(sa.select(tables['import_runs'].c.id).where(tables['import_runs'].c.status=='committed')).scalars())
            known_task_ids=set(connection.execute(sa.select(tables['tasks'].c.id)).scalars())
            for source,records in parsed:
                for table,key,expected,digest in records:
                    logical=fingerprint([IDENTITY_NAMESPACE,source['kind'],source['relative_path'].casefold(),key,table])
                    mapped=mapping_by_key.get(logical)
                    if not mapped or mapped['payload_fingerprint']!=digest:
                        raise MySQLStorageError('来源记录指纹或映射不一致')
                    row=(task_rows if table=='tasks' else event_rows).get(mapped['target_key'])
                    if row is None or row['import_run_id'] not in committed_ids or row['origin']!='historical':
                        raise MySQLStorageError('目标记录不存在或尚未发布')
                    if table=='tasks':
                        compared=expected
                    else:
                        compared={k:v for k,v in expected.items() if k!='details'}
                        if compared.get('task_id') and compared['task_id'] not in known_task_ids:
                            compared['task_id']=None
                        details=row['details'] or {}
                        if details.get('source_fingerprint')!=digest or any(details.get(k)!=v for k,v in expected['details'].items()):
                            raise MySQLStorageError('历史事件原文指纹或上下文不一致')
                    if any(row[k]!=value for k,value in compared.items()):
                        raise MySQLStorageError('历史目标字段与原快照不一致')
                    checked[table]+=1
            return {'passed':True,'import_run_id':run_id,'verified_records':dict(checked),
                    'source_files_verified':len(parsed),'manifest_sha256':fingerprint(manifest['sources'])}
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None
