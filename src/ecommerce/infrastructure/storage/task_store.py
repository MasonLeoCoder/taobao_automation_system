"""任务与事件的 MySQL 存取；事件唯一键和任务行锁保证补写及游标稳定。"""
from datetime import datetime,timezone
from functools import lru_cache
import hashlib
import json

import sqlalchemy as sa

from ecommerce.infrastructure.storage.mysql import MySQLStorageError,create_mysql_engine,current_revision,connection_status,safe_error
from ecommerce.infrastructure.storage.mysql_migrations.schema_v5 import REVISION,build_metadata
from ecommerce.infrastructure.storage.time_values import BEIJING,database_time,database_now,iso_value


def fingerprint(value):
    """稳定计算记录指纹；参数：JSON 兼容值；返回：SHA-256。"""
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


class MySQLTaskStore:
    """独立于 CLI/Web 的任务和日志存储；所有错误只抛安全摘要。"""
    def __init__(self,settings):
        self.settings=settings
        self.engine=create_mysql_engine(settings)
        self.tables=build_metadata().tables

    def initialize(self):
        """仅检查连接和版本；参数：无；返回：无，不执行建表。"""
        try:
            with self.engine.connect() as connection:
                connection_status(connection)
                if current_revision(connection)!=REVISION:
                    raise MySQLStorageError('任务库结构版本不匹配，请先显式执行 database upgrade')
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None


    def list_tasks(self,limit=50,*,active=False):
        """读取近期或全部活跃任务；参数：数量、是否活跃；返回：兼容旧接口的字典。"""
        table=self.tables['tasks']
        statement=sa.select(table)
        if active:
            statement=statement.where(table.c.origin=='live',table.c.finished_at.is_(None),table.c.status.in_(['running','needs_attention']))
        statement=statement.order_by(table.c.started_at.desc(),table.c.id.desc())
        if not active:
            statement=statement.limit(limit)
        try:
            with self.engine.connect() as connection:
                rows=[dict(r) for r in connection.execute(statement).mappings()]
            for row in rows:
                for key in ('started_at','finished_at','owner_process_started_at'):
                    row[key]=iso_value(row[key])
            return rows
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None

    def task_counts(self):
        """统计正式可见任务；参数：无；返回：状态与数量。"""
        table=self.tables['tasks']
        try:
            with self.engine.connect() as connection:
                return dict(connection.execute(sa.select(table.c.status,sa.func.count()).group_by(table.c.status)).all())
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None

    def apply_operation(self,operation):
        """原子补写任务变更与事件；参数：已持久化操作；返回：事件序号或空值。"""
        tasks,events=self.tables['tasks'],self.tables['task_events']
        if operation.get('database',self.settings.database)!=self.settings.database:
            raise MySQLStorageError('本地流水属于另一数据库，未跨库补写')
        if operation['op'] not in {'create','event','progress','finish'}:
            raise MySQLStorageError('流水操作类型无效，未执行写入')
        task_id=operation.get('task_id')
        digest=fingerprint(operation)
        try:
            with self.engine.begin() as connection:
                task=None
                if task_id:
                    task=connection.execute(sa.select(tasks).where(tasks.c.id==task_id).with_for_update()).mappings().one_or_none()
                if operation['op']=='create' and task is None:
                    values=dict(operation['task'])
                    for key in ('started_at','owner_process_started_at'):
                        values[key]=database_time(values.get(key))
                    connection.execute(tasks.insert(),values)
                    task=connection.execute(sa.select(tasks).where(tasks.c.id==task_id).with_for_update()).mappings().one()
                if task_id and task is None:
                    raise MySQLStorageError('补写事件的任务不存在，未制造任务结果')
                existing=connection.execute(sa.select(events.c.task_sequence,events.c.details).where(events.c.event_key==operation['event_key'])).mappings().one_or_none()
                if existing:
                    if (existing['details'] or {}).get('operation_fingerprint')!=digest:
                        raise MySQLStorageError('同一事件键内容冲突，请核对本地流水')
                    return existing['task_sequence']
                if task and task['origin']!='live':
                    raise MySQLStorageError('历史任务不可接受实时执行或恢复写入')
                if task and task['finished_at'] is not None and operation['op'] in {'progress','finish','create'}:
                    raise MySQLStorageError('任务已经结束，未覆盖已提交的结论')
                sequence=task['last_event_sequence']+1 if task else None
                details=dict(operation.get('details') or {})
                details['operation_fingerprint']=digest
                connection.execute(events.insert(),{'task_id':task_id,'task_sequence':sequence,'event_type':operation['event_type'],
                    'level':operation.get('level','INFO'),'logger_name':operation.get('logger_name'),'step':operation.get('step'),
                    'message':operation.get('message'),'state':operation.get('state'),'occurred_at':database_time(operation['time']),
                    'recorded_at':database_now(),'time_raw':operation['time'],'time_basis':'explicit_offset',
                    'details':details,'origin':'live','event_key':operation['event_key']})
                if task:
                    change={'last_event_sequence':sequence}
                    if operation['op']=='finish':
                        change.update(status=operation['state'],finished_at=database_time(operation['time']),message=operation.get('message'))
                    elif operation['op']=='progress' and task['finished_at'] is None:
                        change.update(status=operation['state'],message=operation.get('message'))
                    connection.execute(tasks.update().where(tasks.c.id==task_id).values(**change))
                return sequence
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None

    def recover_interrupted(self,execution_key,is_alive):
        """在调用者持有资源锁时恢复失效任务；参数：资源键、进程核对函数；返回：恢复数量。"""
        from uuid import uuid4
        recovered=0
        for task in self.list_tasks(active=True):
            if task['execution_key']!=execution_key or is_alive(task['owner_pid'],task['owner_process_started_at']) is not False:
                continue
            self.apply_operation({'op':'finish','task_id':task['id'],'event_key':'recovery:'+uuid4().hex,
                'time':datetime.now(BEIJING).isoformat(),'event_type':'task_recovered','state':'needs_attention','level':'WARNING',
                'message':'上次执行进程已失效，请核对已完成的页面操作与保存结果后重新运行'})
            recovered+=1
        return recovered

    def list_events(self,*,task_id=None,after=0,limit=100,level=None,since=None,until=None):
        """分页读取已提交事件；参数：任务、游标、数量、级别和带时区范围；返回：事件及下一游标。"""
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=500:
            raise ValueError('事件游标须非负，数量须为 1–500')
        if level and level not in {'DEBUG','INFO','WARNING','ERROR','CRITICAL'}:
            raise ValueError('日志级别不受支持')
        import re
        if task_id and not re.fullmatch('[a-f0-9]{32}',task_id):
            raise ValueError('任务编号格式无效')
        if since and until and database_time(since)>database_time(until):
            raise ValueError('开始时间不能晚于结束时间')
        table=self.tables['task_events']
        cursor=table.c.task_sequence if task_id else table.c.id
        statement=sa.select(table).where(cursor>after)
        if task_id:
            statement=statement.where(table.c.task_id==task_id)
        if level:
            statement=statement.where(table.c.level==level)
        if since:
            statement=statement.where(table.c.occurred_at>=database_time(since))
        if until:
            statement=statement.where(table.c.occurred_at<=database_time(until))
        statement=statement.order_by(cursor).limit(limit)
        try:
            with self.engine.connect() as connection:
                rows=[dict(r) for r in connection.execute(statement).mappings()]
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None
        for row in rows:
            for key in ('occurred_at','recorded_at'):
                row[key]=iso_value(row[key])
            # 页面/公共查询只提供摘要，不推送签名 URL、原始异常上下文及内部流水指纹。
            row.pop('details',None)
        return {'events':rows,'next_cursor':rows[-1]['task_sequence' if task_id else 'id'] if rows else after,
                'cursor_type':'task_sequence' if task_id else 'historical_id','task_id':task_id}


@lru_cache(maxsize=4)
def task_store(settings):
    """按配置复用小容量连接池；参数：不可变 MySQL 配置；返回：任务存储。"""
    return MySQLTaskStore(settings)
