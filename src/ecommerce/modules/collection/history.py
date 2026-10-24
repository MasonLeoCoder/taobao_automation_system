"""采集历史的固定备份、预览、整批导入和逐记录核对，不执行采集动作。"""
from collections import Counter
from contextlib import closing,contextmanager,ExitStack
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_snapshot import digest_file,owned_path,open_snapshot
from ecommerce.infrastructure.storage.collection_store import CollectionWriter,collection_store,now,database_values
from ecommerce.infrastructure.storage.mysql_migrations.schema_v3 import build_metadata
from ecommerce.infrastructure.storage.mysql import MySQLStorageError,safe_error
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.modules.collection.database_records import MODULE_FILES,SECTIONS,build_snapshot
from ecommerce.modules.collection.persistence import validate_publication

PARSER_VERSION='collection_history_v1'
TARGET_TABLES={'products','collection_runs','collection_steps','collection_batches','product_current_sections',
    'search_records','list_observations','sku_observations','parameter_observations','review_summaries',
    'review_observations','question_observations','answer_observations','file_assets','media_references','collection_checkpoints'}


@contextmanager
def history_lock(settings):
    """排除两平台采集并发；参数：配置；返回：固定顺序持有全部浏览器资源锁的作用域。"""
    profiles={settings.profile_path.resolve(),settings.for_platform('taobao').profile_path.resolve()}
    with ExitStack() as stack:
        for profile in sorted(profiles,key=str):
            stack.enter_context(shop_lock(profile))
        yield


def snapshot_history(settings):
    """在调用方持执行资源锁时备份；参数：配置；返回：固定清单路径，保留全部原件。"""
    if settings.collection_backend!='files':
        raise MySQLStorageError('采集已切换；复核必须使用切换前快照，不能重新全扫实时导出')
    destination=settings.workspace/'backups'/('collection_history_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8])
    destination.mkdir(parents=True,exist_ok=False)
    source_paths=set()
    jobs=[]
    roots={}
    for folder,kind,module in [('taobao_search','page','search'),('taobao_detail_open','page','detail_open'),('taobao_products','batch','list')]:
        for path in sorted((settings.workspace/'output/raw'/folder).glob('*.json')):
            jobs.append({'path':path,'kind':kind,'module':module,'context':{}})
        source_paths.update((settings.workspace/'output/raw'/folder).glob('*.*'))
    details=settings.workspace/'output/raw/taobao_details'
    for directory in sorted(details.iterdir()) if details.exists() else []:
        if not directory.is_dir() or not directory.name.isdigit():
            continue
        source_paths.update(p for p in directory.rglob('*') if p.is_file() and not p.name.endswith(('.tmp','-wal','-shm')))
        root=directory/(directory.name+'.json')
        document=json.loads(root.read_text(encoding='utf-8')) if root.exists() else {}
        if document and document.get('product_id')!=directory.name:
            raise ValueError('商品根文档与资料目录身份不一致')
        roots[directory.name]=document
        reports=[]
        for path in sorted((directory/'history').glob('*/collection_summary.json')):
            record=json.loads(path.read_text(encoding='utf-8'))
            reports.append(record)
            jobs.append({'path':path,'kind':'report','module':None,'context':{}})
        for module,filename in MODULE_FILES.items():
            paths=list((directory/'history').glob('*/'+filename))
            if module=='skus':
                paths+= [p for p in (directory/'history').glob('*/sku_progress.json') if not (p.parent/'sku_details.json').exists()]
            if module in {'reviews','questions'}:
                paths += [p for p in (directory/'history').glob('*/'+module+'-progress.json') if not (p.parent/(module+'.json')).exists()]
            for path in sorted(paths):
                record=json.loads(path.read_text(encoding='utf-8'))
                batch=record['batch_id']
                sections=[]
                last_attempt=False
                for section,value in document.items():
                    if not isinstance(value,dict) or value.get('batch_id')!=batch:
                        continue
                    normal=section.removesuffix('_last_attempt')
                    if SECTIONS.get(normal)!=module:
                        continue
                    if section.endswith('_last_attempt'):
                        last_attempt=True
                    else:
                        sections.append(section)
                if module=='skus' and 'sku_details' in sections and document.get('product_info'):
                    sections.append('product_info')
                validate_publication(module,record,sections)
                owners=[r for r in reports if r.get('task_id')==record.get('task_id') and r.get('product_id')==record.get('product_id')
                        and any(step['name']==module and step['requested'] for step in r['steps'])]
                if len(owners)>1:
                    raise ValueError('模块批次存在多个可能运行归属，未猜测关联')
                context={'sections':sections,'last_attempt':last_attempt,
                         'report_key':owners[0]['batch_id'] if owners else None}
                jobs.append({'path':path,'kind':'batch','module':module,'context':context})
    # 汇总先创建，派生批次随后按其明确来源拓扑排序。
    jobs.sort(key=lambda job:(job['kind']=='batch',job['kind']!='report',str(job['path'])))
    ordered=[]
    batch_jobs={}
    for job in jobs:
        record=json.loads(job['path'].read_text(encoding='utf-8'))
        job['record']=record
        if job['kind']=='batch':
            key=(job['module'],record['batch_id'])
            if key in batch_jobs:
                raise ValueError('来源中存在重复批次身份，未合并不同原件')
            batch_jobs[key]=job
        else:
            ordered.append(job)
    visiting=set()
    visited=set()
    def visit(key):
        """核对派生关系无环；参数：模块与原批次；返回：无。"""
        if key in visiting:
            raise ValueError('采集派生批次出现循环')
        if key in visited:
            return
        visiting.add(key)
        job=batch_jobs[key]
        upstream=job['record'].get('source_batch_id') or job['record'].get('resumed_from_batch_id')
        parent=(job['module'],upstream)
        if parent in batch_jobs:
            visit(parent)
        visiting.remove(key)
        visited.add(key)
        ordered.append(job)
    for key in batch_jobs:
        visit(key)
    documents=[]
    for job in ordered:
        version=job['record'].get('schema_version')
        if version not in {None,1,2}:
            raise ValueError('来源声明了尚不支持的采集格式版本')
        reference=build_snapshot(settings,job['kind'],job['record'],job['path'],module=job['module'],directory=destination/'documents')
        header,rows=open_snapshot(settings,reference)
        for record in rows:
            file=record.get('file')
            if file and file['availability']=='present':
                source_paths.add(settings.workspace/file['relative_path'])
        documents.append({'reference':reference,'logical_key':header['logical_key'],'kind':job['kind'],'module':job['module'],
                          'source_relative_path':header['source_relative_path'],'context':job['context']})
    sources=[]
    for original in sorted(source_paths,key=str):
        original=owned_path(settings.workspace,original)
        relative=original.relative_to(settings.workspace.resolve()).as_posix()
        target=destination/'files'/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        source_digest=digest_file(original)
        declared=None
        kind='file'
        if original.suffix=='.sqlite3':
            with closing(sqlite3.connect(original.as_uri()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
                declared=source.execute('PRAGMA user_version').fetchone()[0] or None
            kind='module_working_sqlite'
        else:
            shutil.copy2(original,target)
            if original.suffix=='.json' and original.parent.name not in {'用户评价','问大家'}:
                declared=json.loads(original.read_text(encoding='utf-8')).get('schema_version')
                kind='metadata_json'
        if digest_file(original)!=source_digest:
            raise ValueError('备份期间原采集文件发生变化，未使用混合来源')
        sources.append({'relative_path':relative,'source_sha256':source_digest,'sha256':digest_file(target),
            'size_bytes':target.stat().st_size,'snapshot':target.relative_to(destination).as_posix(),
            'source_kind':kind,'schema_version':declared})
    issues=[]
    entry_products={job['record']['product_id'] for job in jobs if job['kind']=='page' and job['module']=='detail_open'}
    detail_products={job['record']['product_id'] for job in jobs if job['kind']=='batch' and job['module']!='list'}
    for item_id in sorted(entry_products-detail_products):
        issues.append({'severity':'info','code':'entry_without_detail_data',
            'message':'商品 '+item_id+' 仅有详情入口记录，没有现存详情采集资料；未制造明细'})
    manifest={'parser_version':PARSER_VERSION,'scope':'collection_history','created_at':datetime.now(timezone.utc).isoformat(),
              'sources':sources,'documents':documents,'issues':issues}
    path=destination/'manifest.json'
    with path.open('x',encoding='utf-8') as handle:
        json.dump(manifest,handle,ensure_ascii=False,indent=2)
    return path


def read_manifest(settings,path):
    """核对固定来源与快照；参数：配置和清单；返回：已校验清单及稳定版本哈希。"""
    path=Path(path).resolve()
    manifest=json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('parser_version')!=PARSER_VERSION or manifest.get('scope')!='collection_history':
        raise ValueError('采集历史清单版本或范围不匹配')
    identities=set()
    for source in manifest['sources']:
        target=owned_path(path.parent,source['snapshot'])
        if source['relative_path'] in identities:
            raise ValueError('采集快照包含重复来源文件')
        identities.add(source['relative_path'])
        if target.stat().st_size!=source['size_bytes'] or digest_file(target)!=source['sha256']:
            raise ValueError('采集来源快照大小或哈希不一致')
    for document in manifest['documents']:
        target=owned_path(settings.workspace,document['reference']['relative_path'])
        target.relative_to(path.parent)
        header,iterator=open_snapshot(settings,document['reference'])
        iterator.close()
        if header['logical_key']!=document['logical_key'] or header['source_relative_path']!=document['source_relative_path']:
            raise ValueError('采集快照与清单的记录身份不一致')
    identity={'sources':[{k:v for k,v in item.items() if k!='snapshot'} for item in manifest['sources']],
              'documents':[{k:v for k,v in item.items() if k!='reference'}|{'content_sha256':item['reference']['sha256']} for item in manifest['documents']]}
    return manifest,fingerprint(identity)


def preview_history(settings,path):
    """只读预览采集范围；参数：配置与固定清单；返回：数量、范围及已知限制。"""
    manifest,digest=read_manifest(settings,path)
    counts=Counter()
    products=set()
    files=set()
    coverage=Counter()
    tables=build_metadata().tables
    for document in manifest['documents']:
        header,rows=open_snapshot(settings,document['reference'])
        root_table={'batch':'collection_batches','page':'search_records','report':'collection_runs'}[header['kind']]
        database_values(tables[root_table],header['values'])
        counts[root_table]+=1
        if header.get('product_id'):
            products.add(header['product_id'])
        if header['kind']=='batch':
            coverage[header['module']+':'+header['values']['coverage_status']]+=1
            counts['product_current_sections']+=len(document['context'].get('sections',[]))
        for record in rows:
            database_values(tables[record['table']],record['values'])
            counts[record['table']]+=1
            if record.get('product_id'):
                products.add(record['product_id'])
            if record.get('file'):
                files.add(record['file']['relative_path'])
    counts['products']=len(products)
    counts['file_assets']=len(files)
    return {'preview':True,'parser_version':PARSER_VERSION,'manifest':str(Path(path).resolve()),'manifest_sha256':digest,
            'source_files':len(manifest['sources']),'counts':dict(counts),'coverage':dict(coverage),'issues':manifest['issues']}


def _track(connection,tables,source_id,document):
    """创建映射登记器；参数：连接、表、来源、文档；返回：稳定身份核对回调。"""
    table=tables['import_row_mappings']
    def record(target,key,pk,digest,matched=False):
        """登记目标映射；参数：受控表、源键、主键及内容指纹；返回：无。"""
        if target not in TARGET_TABLES:
            raise MySQLStorageError('采集导入目标表不受支持')
        # key 中的数据库内部关联只作为目标信息；逻辑根稳定标识与业务唯一列共同定位。
        stable={k:v for k,v in key.items() if k not in {'batch_id','run_id','question_id','id','product_id'}}
        if not stable and target not in {'review_summaries','product_current_sections'}:
            stable={'root':document['logical_key']}
        logical=fingerprint(['collection_row_v1',document['logical_key'],target,stable])
        conflict=connection.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.logical_key==logical,table.c.payload_fingerprint!=digest))
        if conflict:
            raise MySQLStorageError('同一采集来源记录内容冲突，整批未覆盖')
        existing=connection.execute(sa.select(table.c.target_key,table.c.payload_fingerprint).where(
            table.c.source_id==source_id,table.c.logical_key==logical)).mappings().one_or_none()
        if existing:
            if existing['target_key']!=pk or existing['payload_fingerprint']!=digest:
                raise MySQLStorageError('同一源记录映射到不同目标，未覆盖')
            return
        source_key=json.dumps({'document':document['logical_key'],'table':target,'key':stable},ensure_ascii=False,sort_keys=True)
        connection.execute(table.insert().values(source_id=source_id,source_record_key=source_key,
            source_record_hash=fingerprint(source_key),logical_key=logical,target_table=target,target_key=pk,
            target_key_hash=fingerprint(pk),payload_fingerprint=digest,result='matched' if matched else 'inserted'))
    return record


def import_history(settings,path):
    """整批核对后发布采集历史；参数：配置、固定清单；返回：新增数量与隔离/缺失说明。"""
    manifest,digest=read_manifest(settings,path)
    store=collection_store(settings)
    store.tasks.initialize()
    tables=store.tables
    runs=tables['import_runs']
    run_id=None
    lock='collections_'+fingerprint(settings.mysql.database)[:40]
    try:
        with store.engine.connect() as connection:
            if connection.scalar(sa.text('SELECT GET_LOCK(:key,0)'),{'key':lock})!=1:
                raise MySQLStorageError('另一个采集历史导入正在执行')
            connection.commit()
            try:
                old=connection.execute(sa.select(runs).where(runs.c.manifest_sha256==digest,runs.c.parser_version==PARSER_VERSION,runs.c.status=='committed')).mappings().one_or_none()
                connection.commit()
                if old:
                    return {'status':'committed','already_committed':True,'import_run_id':old['id'],'counts':old['counts'],'manifest':str(path)}
                with connection.begin():
                    run_id=connection.execute(runs.insert().values(started_at=now(),status='running',manifest_sha256=digest,
                        parser_version=PARSER_VERSION,scope={'kind':'collection_history','manifest':str(Path(path).resolve())})).inserted_primary_key[0]
                with connection.begin():
                    before={name:connection.scalar(sa.select(sa.func.count()).select_from(tables[name])) for name in TARGET_TABLES}
                    source_ids={}
                    for source in manifest['sources']:
                        source_ids[source['relative_path']]=connection.execute(tables['import_sources'].insert().values(import_run_id=run_id,
                            root_key='workspace',relative_path=source['relative_path'],sha256=source['sha256'],size_bytes=source['size_bytes'],
                            source_kind=source['source_kind'],schema_version=source['schema_version'],parse_status='parsed')).inserted_primary_key[0]
                    issues=list(manifest['issues'])
                    for document in manifest['documents']:
                        source_id=source_ids[document['source_relative_path']]
                        writer=CollectionWriter(settings,connection,import_run_id=run_id,track=_track(connection,tables,source_id,document))
                        result=writer.apply(document['reference'],document['context'])
                        issues.extend({**issue,'source_id':source_id} for issue in result['issues'])
                    # 用相同规范源逐字段重读，核对完成后才发布导入根。
                    for document in manifest['documents']:
                        CollectionWriter(settings,connection,import_run_id=run_id,check_only=True).apply(document['reference'],document['context'])
                    for issue in issues:
                        connection.execute(tables['import_issues'].insert().values(import_run_id=run_id,source_id=issue.get('source_id'),
                            severity=issue['severity'],code=issue['code'],message=issue['message'],resolution='原状态与原件保留；未制造缺失数据'))
                    after={name:connection.scalar(sa.select(sa.func.count()).select_from(tables[name])) for name in TARGET_TABLES}
                    counts={'inserted':{name:after[name]-before[name] for name in sorted(TARGET_TABLES)},'issues':len(issues),
                        'conflicts':0,'quarantined_batches':connection.scalar(sa.select(sa.func.count()).select_from(tables['collection_batches']).where(
                            tables['collection_batches'].c.import_run_id==run_id,tables['collection_batches'].c.visibility=='quarantined'))}
                    connection.execute(runs.update().where(runs.c.id==run_id).values(status='committed',finished_at=now(),counts=counts))
                return {'status':'committed','already_committed':False,'import_run_id':run_id,'counts':counts,'issues':issues,
                        'manifest':str(Path(path).resolve()),'manifest_sha256':digest}
            except Exception:
                connection.rollback()
                if run_id:
                    with connection.begin():
                        updated=connection.execute(runs.update().where(runs.c.id==run_id,runs.c.status!='committed').values(
                            status='failed',finished_at=now(),counts={'rolled_back':True}))
                        if updated.rowcount:
                            connection.execute(tables['import_issues'].insert().values(import_run_id=run_id,severity='error',
                                code='batch_rolled_back',message='采集导入核对未通过，整批业务写入已回滚',resolution='保留固定快照，按源问题修正或核对后重试'))
                raise
            finally:
                connection.execute(sa.text('SELECT RELEASE_LOCK(:key)'),{'key':lock})
                connection.commit()
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def audit_history(settings,path):
    """只读逐字段复核历史；参数：配置、原快照；返回：已核对的文档数量。"""
    manifest,digest=read_manifest(settings,path)
    store=collection_store(settings)
    try:
        with store.engine.connect() as connection:
            run=connection.execute(sa.select(store.tables['import_runs']).where(
                store.tables['import_runs'].c.manifest_sha256==digest,store.tables['import_runs'].c.parser_version==PARSER_VERSION,
                store.tables['import_runs'].c.status=='committed')).mappings().one_or_none()
            if run is None:
                raise MySQLStorageError('没有已发布的采集导入可复核')
            for document in manifest['documents']:
                CollectionWriter(settings,connection,import_run_id=run['id'],check_only=True).apply(document['reference'],document['context'])
        return {'passed':True,'import_run_id':run['id'],'documents_verified':len(manifest['documents']),
                'source_files_verified':len(manifest['sources']),'manifest_sha256':digest}
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None
