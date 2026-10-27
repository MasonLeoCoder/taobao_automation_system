"""仅迁移当前正式派发目录的申请描述，不导入商品资料、不制造恢复计划。"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlalchemy as sa
from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore, identifier, JobConflict
from ecommerce.infrastructure.storage.time_values import database_now, database_time
from ecommerce.infrastructure.tasks.ownership import execution_key
from ecommerce.shared.config import Settings, MySQLSettings


def migrate_dispatch_files(settings):
    """核对来源后幂等迁移，不启动执行；参数：当前正式配置；返回：可审计计数和编号。"""
    store=CollectionJobStore(settings)
    j,t=store.tables['collection_jobs'],store.tables['tasks']
    key=execution_key(settings.for_platform('taobao'))
    report={'created':[],'already_present':[],'enriched':[],'foreign':[],'sources':{}}
    with store.connection() as c:
        if c.scalar(sa.select(sa.func.count()).select_from(t).where(t.c.finished_at.is_(None))):
            raise JobConflict('存在未结束任务，暂不迁移派发描述')
    directory=settings.workspace/'storage/web_collection_jobs'
    for path in sorted(directory.glob('*.json')):
        identifier(path.stem)
        if path.is_symlink() or path.resolve().parent!=directory.resolve():
            raise JobConflict('派发来源路径不受信任，未迁移')
        raw=path.read_bytes()
        row=json.loads(raw.decode('utf-8'))
        if row.get('database')!=settings.mysql.database or row.get('execution_key')!=key:
            report['foreign'].append(path.stem)
            continue
        if row.get('id')!=path.stem:
            raise JobConflict('派发文件编号不一致，未迁移')
        config=dict(row['config'])
        mysql=MySQLSettings(**config['mysql'])
        if mysql!=settings.mysql or Path(config['workspace']).resolve()!=settings.workspace.resolve():
            raise JobConflict('派发数据库或工作空间不匹配，未迁移')
        for name in ('root','workspace','session_path','profile_path','taobao_session_path','taobao_profile_path','qianniu_session_path','qianniu_profile_path'):
            if config.get(name) is not None:
                config[name]=Path(config[name])
        config['mysql']=mysql
        original=Settings(**config)
        if execution_key(original.for_platform('taobao'))!=key:
            raise JobConflict('派发配置与浏览器资源不一致，未迁移')
        digest=hashlib.sha256(raw).hexdigest()
        report['sources'][path.stem]=digest
        with store.connection(True) as c:
            old=c.execute(sa.select(j).where(j.c.id==row['id']).with_for_update()).mappings().first()
            if old and not old['payload'].get('migration_pending'):
                if old['payload'].get('legacy_source_sha256') not in (None,digest):
                    raise JobConflict('已迁移派发来源发生变化，保留现场待核对')
                report['already_present'].append(row['id'])
                continue
            task=c.execute(sa.select(t).where(t.c.id==row['id'])).mappings().first()
            if task and task['execution_key']!=key:
                raise JobConflict('派发与已有执行资源不一致，未关联')
            payload={k:v for k,v in row.items() if k not in {'state','task_status','result','owner_pid','owner_process_started_at','message','created_at','updated_at'}}
            if old and old['payload'].get('migration_pending'):
                legacy=old['payload'].get('legacy_queue') or old['payload']
                if (legacy.get('product_id')!=row.get('product_id') or legacy.get('module') not in row.get('modules',[row.get('module')])
                        or legacy.get('options')!=row.get('options')):
                    raise JobConflict('旧队列与派发文件范围冲突，保留暂停并等待核对')
                payload['legacy_queue']=legacy
            payload['config']=json.loads(json.dumps(asdict(original),default=str))
            payload.update(legacy_source_sha256=digest,legacy_state=row['state'] if row['state'] in {
                'succeeded','failed','cancelled','needs_attention','interrupted','verification_pending'} else 'interrupted')
            values={'payload':payload,'message':row.get('message'),'owner_pid':row.get('owner_pid'),
                'owner_process_started_at':database_time(row.get('owner_process_started_at')),'updated_at':database_now()}
            if old:
                values['revision']=old['revision']+1
                c.execute(j.update().where(j.c.id==row['id']).values(**values))
                report['enriched'].append(row['id'])
            else:
                c.execute(j.insert().values(id=row['id'],execution_key=key,submission_key=None,request_key=None,
                    origin='legacy',dispatch_state='started' if task else 'legacy_finished',task_id=task['id'] if task else None,
                    revision=1,created_at=database_time(row['created_at']),**values))
                report['created'].append(row['id'])
    # 旧的停止凭据只转为数据库暂停，迁移绝不自动领取。
    if report['created'] or report['enriched']:
        store.pause(key,True,'派发记录迁移完成，等待手动选择任务或继续队列')
    return report
