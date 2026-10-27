"""采集计划与清理凭据持久化；不调用浏览器，不从商品全局资料猜测进度。"""
from copy import deepcopy
from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import re
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error
from ecommerce.infrastructure.storage.task_store import task_store, fingerprint
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.storage.collection_snapshot import owned_path, digest_file
from ecommerce.shared.exceptions import NeedsAttention

COMPLETE = {'complete', 'downloaded', 'no_video', 'no_images', 'no_parameters', 'no_reviews', 'no_questions'}


class RecoveryDataError(MySQLStorageError):
    """计划范围或归属冲突不可通过数据库重试解决。"""
    retryable=False


class RecoveryPageError(ValueError,NeedsAttention):
    """列表检查点或原件不能复用，预览拒绝确认，执行时转为需要人工处理。"""


def apply_checkpoint(connection, tables, payload, task_id):
    """同任务事务内幂等保存检查点；参数：连接、表、快照、执行；返回：无。"""
    plans, executions = tables['collection_plans'], tables['collection_plan_executions']
    identifier = payload['id']
    old = connection.execute(sa.select(plans).where(plans.c.id == identifier).with_for_update()).mappings().one_or_none()
    if old:
        if old['options'] != payload['options'] or old['format_version'] != 1:
            raise RecoveryDataError('恢复计划范围或契约版本不一致')
        if old['active_task_id'] != task_id:
            previous = connection.execute(sa.select(tables['tasks']).where(tables['tasks'].c.id == old['active_task_id'])).mappings().one()
            if previous['finished_at'] is None or payload.get('parent_task_id') != old['active_task_id']:
                raise RecoveryDataError('原计划执行未结束或恢复来源已变化')
        if payload['revision'] <= old['revision']:
            return
        connection.execute(plans.update().where(plans.c.id == identifier).values(
            active_task_id=task_id, checkpoint=payload['checkpoint'], revision=payload['revision'], updated_at=database_now()))
    else:
        if identifier != task_id or payload.get('parent_task_id'):
            raise RecoveryDataError('原采集计划不存在，不能猜测恢复')
        connection.execute(plans.insert().values(id=identifier, active_task_id=task_id, format_version=1,
            options=payload['options'], checkpoint=payload['checkpoint'], revision=payload['revision'],
            created_at=database_now(), updated_at=database_now()))
    existing = connection.scalar(sa.select(executions.c.task_id).where(executions.c.task_id == task_id))
    if existing is None:
        connection.execute(executions.insert().values(task_id=task_id, plan_id=identifier,
            parent_task_id=payload.get('parent_task_id'), created_at=database_now()))
    if payload['checkpoint'].get('finished') and payload['checkpoint'].get('cleanup_tasks'):
        # 补采结果与解除原待处理状态同事务提交，保留原错误及来源。
        source_plans=set(connection.scalars(sa.select(executions.c.plan_id).where(
            executions.c.task_id.in_(payload['checkpoint']['cleanup_tasks']),executions.c.plan_id!=identifier)))
        for source_id in sorted(source_plans):
            original=connection.execute(sa.select(plans).where(plans.c.id==source_id).with_for_update()).mappings().one()
            active=connection.scalar(sa.select(tables['tasks'].c.finished_at).where(tables['tasks'].c.id==original['active_task_id']))
            if active is None:
                raise RecoveryDataError('原计划仍在执行，不能并发解除其错误状态')
            checkpoint=deepcopy(original['checkpoint'])
            changed=False
            for product,modules in payload['checkpoint']['products'].items():
                for module,value in modules.items():
                    previous=checkpoint['products'].get(product,{}).get(module)
                    if not previous or previous.get('state') not in {'failed_skipped','not_run_skipped'} or value.get('state') not in {'complete','no_entry'}:
                        continue
                    if module in {'reviews','questions'}:
                        option='all_reviews' if module=='reviews' else 'all_questions'
                        if bool(original['options'].get(option))!=bool(payload['options'].get(option)):
                            continue
                    previous.update(resolved_by_task_id=task_id,resolved_state=value['state'])
                    changed=True
            if changed:
                values=[v for modules in checkpoint['products'].values() for v in modules.values()]
                counts=dict(checkpoint.get('counts') or {})
                counts.update(failed=sum(v.get('state')=='failed_skipped' and not v.get('resolved_by_task_id') for v in values),
                    not_run=sum(v.get('state') in {'running','cleaning','not_run_skipped'} and not v.get('resolved_by_task_id') for v in values),
                    resolved=sum(bool(v.get('resolved_by_task_id')) for v in values))
                checkpoint['counts']=counts
                connection.execute(plans.update().where(plans.c.id==source_id).values(checkpoint=checkpoint,
                    revision=original['revision']+1,updated_at=database_now()))


class RecoveryStore:
    """复用任务连接池，提供检查点读取及受限清理；申请队列由独立存储管理。"""
    def __init__(self, settings):
        self.settings = settings
        self.tasks = task_store(settings.mysql)
        self.engine, self.tables = self.tasks.engine, self.tasks.tables

    @contextmanager
    def connection(self,write=False):
        """统一转换数据库异常，避免泄露SQL和阻止错误降级；参数：写事务；返回：连接。"""
        try:
            with (self.engine.begin() if write else self.engine.connect()) as connection:
                yield connection
        except sa.exc.SQLAlchemyError as error:
            raise MySQLStorageError(safe_error(error)) from None

    def validate_result(self,task_id,product_id,module):
        """复用前核对原批次和全部登记文件；参数：原执行、商品、模块；返回：无。"""
        b,p,r,a=(self.tables[n] for n in ('collection_batches','products','media_references','file_assets'))
        with self.connection() as connection:
            batch=connection.execute(sa.select(b).join(p,b.c.product_id==p.c.id).where(b.c.task_id==task_id,
                p.c.platform=='taobao',p.c.platform_product_id==product_id,b.c.module==module,b.c.status.in_(COMPLETE),
                b.c.visibility=='published').order_by(b.c.id.desc())).mappings().first()
            if not batch:
                raise ValueError('原计划完成批次不存在或已失效，请单模块重新采集')
            files=list(connection.execute(sa.select(a).join(r,r.c.file_asset_id==a.c.id).where(r.c.batch_id==batch['id'])).mappings())
        for record in files:
            path=owned_path(self.settings.workspace,record['relative_path'])
            if (not path.is_file() or record['availability']!='present' or path.stat().st_size!=record['size_bytes']
                    or digest_file(path)!=record['sha256']):
                raise ValueError('原计划已完成资料文件缺失或变化，请核对后单模块重采')

    def read(self, task_id):
        """按任一次执行读取原计划；参数：任务；返回：计划或空。"""
        p, e = self.tables['collection_plans'], self.tables['collection_plan_executions']
        with self.connection() as connection:
            row = connection.execute(sa.select(p).join(e, e.c.plan_id == p.c.id).where(e.c.task_id == task_id)).mappings().one_or_none()
            return dict(row) if row else None

    def validate_list_page(self, plan_id, number, saved, options):
        """只读核验原计划列表页，缺哈希时使用原文件登记补足；参数：计划、页、描述、范围；返回：完整描述。"""
        b,e,r,a,l=(self.tables[n] for n in ('collection_batches','collection_plan_executions','media_references','file_assets','list_observations'))
        def invalid(reason):
            """生成定位到页的核验错误；参数：原因；返回：异常。"""
            return RecoveryPageError(f'第{number}页列表复用校验失败：{reason}')
        if (not isinstance(saved,dict) or type(number) is not int or number<1 or options.get('mode')!='keyword'
                or number>options['pages'] or saved.get('page_number')!=number):
            raise invalid('页码与原计划不一致')
        with self.connection() as connection:
            batch=connection.execute(sa.select(b).join(e,e.c.task_id==b.c.task_id).where(
                e.c.plan_id==plan_id,b.c.source_batch_key==saved.get('source_batch_key'),
                b.c.platform=='taobao',b.c.module=='list')).mappings().one_or_none()
            if (not batch or batch['page_number']!=number or batch['search_keyword']!=options['keyword']
                    or batch['requested_limit']!=options['limit'] or batch['visibility']!='published'
                    or batch['status'] not in {'limit_reached','loaded_cards_below_limit','all_loaded_cards_collected','empty'}):
                raise invalid('原批次归属、采集范围或完成状态不一致')
            files=connection.execute(sa.select(a).join(r,r.c.file_asset_id==a.c.id).where(
                r.c.batch_id==batch['id'],r.c.task_id==batch['task_id'],r.c.role=='source_json')).mappings().all()
            count=connection.scalar(sa.select(sa.func.count()).select_from(l).where(l.c.batch_id==batch['id']))
        if len(files)!=1:
            raise invalid('缺少唯一的原件登记，无法补足检查点校验值')
        file=files[0]
        try:
            path=owned_path(self.settings.workspace,saved['json_path'])
            registered=owned_path(self.settings.workspace,file['relative_path'])
        except (KeyError,TypeError,ValueError):
            raise invalid('检查点原件路径无效') from None
        if path!=registered or file['root_key']!='workspace' or file['availability']!='present':
            raise invalid('检查点路径与原件登记不一致或原件登记已失效')
        digest=file['sha256']
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
            raise invalid('原件登记缺少可信校验值')
        if saved.get('json_sha256') is not None and saved['json_sha256']!=digest:
            raise invalid('检查点校验值与原件登记不一致')
        try:
            if not path.is_file():raise invalid('原件文件不存在')
            raw=path.read_bytes()
            if len(raw)!=file['size_bytes'] or hashlib.sha256(raw).hexdigest()!=digest:
                raise invalid('原件文件内容与登记校验值不一致')
            record=json.loads(raw.decode('utf-8'))
        except OSError:
            raise invalid('原件文件无法读取，请检查占用或权限') from None
        except (TypeError,UnicodeError,json.JSONDecodeError):
            raise invalid('原件JSON格式无效') from None
        items=record.get('items') if isinstance(record,dict) else None
        if (not isinstance(items,list) or any(not isinstance(item,dict) for item in items)
                or record.get('task_id')!=batch['task_id'] or record.get('batch_id')!=batch['source_batch_key']
                or record.get('keyword')!=options['keyword'] or record.get('page_number')!=number
                or record.get('requested_limit')!=options['limit'] or record.get('collected_count')!=len(items)
                or saved.get('collected_count')!=len(items) or count!=len(items)
                or saved.get('product_ids')!=[item.get('product_id') for item in items]):
            raise invalid('原件内容、商品清单或已入库数量与检查点不一致')
        return {**deepcopy(saved),'json_sha256':digest}

    def lineage(self, plan_id):
        """读取本计划全部执行；参数：原计划；返回：任务编号列表。"""
        table = self.tables['collection_plan_executions']
        with self.connection() as connection:
            return list(connection.scalars(sa.select(table.c.task_id).where(table.c.plan_id == plan_id)))

    def batches(self, task_ids, product_id, module):
        """定位原执行未完成批次，含尚未落库的模块工作目录；参数：任务范围、商品、模块；返回：清理候选。"""
        batches, products = self.tables['collection_batches'], self.tables['products']
        with self.connection() as connection:
            rows = [dict(r) for r in connection.execute(sa.select(batches).join(products, batches.c.product_id == products.c.id).where(
                batches.c.task_id.in_(task_ids), products.c.platform == 'taobao', products.c.platform_product_id == product_id,
                batches.c.module == module)).mappings()]
            audit=self.tables['collection_discarded_batches']
            cleaned=set(connection.scalars(sa.select(audit.c.source_batch_key).where(audit.c.stage=='complete')))
        protected={r['source_batch_key'] for r in rows if r['status'] in COMPLETE}
        rows=[r for r in rows if r['source_batch_key'] not in protected|cleaned]
        protected|=cleaned
        known = {r['source_batch_key'] for r in rows}
        from ecommerce.shared.collection_contract import MODULE_FILES
        directory = self.settings.workspace / 'output/raw/taobao_details' / product_id / 'history'
        # 不递归猜测删除：必须有该模块自己的记录，且任务/商品/批次均能核实。
        names=['.collection-owner.json',MODULE_FILES[module]]+({'skus':['sku_progress.json'],'reviews':['reviews-progress.json'],'questions':['questions-progress.json']}.get(module,[]))
        for path in (p for name in names for p in directory.glob('*/'+name)):
            if path.parent.name in known or path.parent.name in protected:
                continue
            value = json.loads(path.read_text(encoding='utf-8'))
            if (value.get('task_id') in task_ids and value.get('product_id') == product_id
                    and (path.name!='.collection-owner.json' or value.get('module')==module)
                    and value.get('batch_id') == path.parent.name and value.get('video_status', value.get('status')) not in COMPLETE):
                rows.append({'source_batch_key': path.parent.name, 'task_id': value['task_id'], 'module': module})
                known.add(path.parent.name)
        return rows

    def cleanup(self, task_id, source, product_id, module):
        """事务删除旧明细并按清单清理专属文件；参数：新执行、旧批次及归属；返回：删除计数，可幂等重入。"""
        from ecommerce.infrastructure.storage.collection_store import PRUNE_ORDER
        tables = self.tables
        discarded, batches, assets, refs = (tables[n] for n in ('collection_discarded_batches','collection_batches','file_assets','media_references'))
        key = source['source_batch_key']
        # 原编号不可成为任意路径；history内每批独占目录，不处理符号链接。
        if not key.startswith(source['task_id'] + '-') or any(c in key for c in '/\\:'):
            raise ValueError('旧批次目录与任务编号不匹配')
        directory = owned_path(self.settings.workspace, Path('output/raw/taobao_details') / product_id / 'history' / key)
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in [directory, *directory.parents] if p != self.settings.workspace.parent):
            raise ValueError('清理目录包含链接，拒绝沿链接删除')
        with self.connection(write=True) as connection:
            source_task=connection.execute(sa.select(tables['tasks']).where(tables['tasks'].c.id==source['task_id']).with_for_update()).mappings().one_or_none()
            if not source_task or source_task['finished_at'] is None:
                raise ValueError('旧任务尚未结束，不能清理仍可能写入的批次')
            audit = connection.execute(sa.select(discarded).where(discarded.c.source_batch_key == key).with_for_update()).mappings().one_or_none()
            if audit:
                if (audit['source_task_id'], audit['product_id'], audit['module']) != (source['task_id'], product_id, module):
                    raise ValueError('清理凭据归属不一致')
                manifest, counts = audit['manifest'], dict(audit['counts'])
            else:
                batch = connection.execute(sa.select(batches).where(batches.c.source_batch_key == key, batches.c.platform == 'taobao', batches.c.module == module).with_for_update()).mappings().one_or_none()
                if batch:
                    external = connection.scalar(sa.select(tables['products'].c.platform_product_id).where(tables['products'].c.id == batch['product_id']))
                    if batch['task_id'] != source['task_id'] or external != product_id or batch['status'] in COMPLETE:
                        raise ValueError('旧结果已完成或归属变化，拒绝清理')
                    if connection.scalar(sa.select(sa.func.count()).select_from(tables['product_current_sections']).where(tables['product_current_sections'].c.batch_id == batch['id'])):
                        raise ValueError('旧批次仍为有效资料，拒绝清理')
                manifest, counts = [], {'files': 0, 'shared': 0}
                candidates = list(directory.rglob('*')) if directory.exists() else []
                for path in candidates:
                    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                        raise ValueError('批次包含链接，拒绝清理')
                    if not path.is_file():
                        continue
                    relative = owned_path(self.settings.workspace, path).relative_to(self.settings.workspace).as_posix()
                    asset = connection.execute(sa.select(assets).where(assets.c.path_key == __import__('hashlib').sha256(relative.casefold().encode()).hexdigest())).mappings().one_or_none()
                    shared = False
                    if asset:
                        for table in tables.values():
                            for foreign in table.foreign_keys:
                                if foreign.column.table.name != 'file_assets':
                                    continue
                                condition = foreign.parent == asset['id']
                                if table.name == 'media_references' and batch:
                                    condition = sa.and_(condition, sa.or_(refs.c.batch_id != batch['id'], refs.c.batch_id.is_(None)))
                                if connection.scalar(sa.select(sa.func.count()).select_from(table).where(condition)):
                                    shared = True
                    if shared:
                        counts['shared'] += 1
                        continue
                    if asset and ((asset['sha256'] and digest_file(path)!=asset['sha256']) or
                                  (asset['size_bytes'] is not None and path.stat().st_size!=asset['size_bytes'])):
                        raise ValueError('已登记的旧批次文件被修改，拒绝删除，请人工核对')
                    manifest.append({'path': relative, 'size': path.stat().st_size, 'sha256': digest_file(path), 'asset_id': asset['id'] if asset else None})
                if batch:
                    for name in PRUNE_ORDER:
                        table = tables[name]
                        counts[name] = connection.execute(table.delete().where(table.c.batch_id == batch['id'])).rowcount
                    # 只保留批次身份、原任务归属和清理依据，不保存未完成结果正文。
                    connection.execute(batches.update().where(batches.c.id == batch['id']).values(status='discarded', visibility='quarantined',
                        result_payload={'discarded': True, 'cleanup_task_id': task_id, '_current_sections': [], '_export_sections': [], '_last_attempt_selected': False}))
                connection.execute(discarded.insert().values(source_batch_key=key, source_task_id=source['task_id'], cleanup_task_id=task_id,
                    product_id=product_id, module=module, stage='records_deleted', manifest=manifest, counts=counts,
                    created_at=database_now(), updated_at=database_now()))
        # 数据库和文件分阶段提交；只逐文件删除，校验每个最终绝对路径和原内容。
        deleted = 0
        for item in manifest:
            path = owned_path(self.settings.workspace, item['path'])
            path.relative_to(directory)
            if path.exists():
                if path.is_symlink() or path.stat().st_size != item['size'] or digest_file(path) != item['sha256']:
                    raise ValueError('待清理文件已变化，保留现场，未开始新采集')
                path.unlink()
            deleted += 1
        # 最后尝试分区只移除目标批次，其他分区和正式结果原样保留。
        document_path = directory.parent.parent / f'{product_id}.json'
        if document_path.exists():
            from ecommerce.infrastructure.tasks.ownership import atomic_json
            document = json.loads(document_path.read_text(encoding='utf-8'))
            document = {k: v for k, v in document.items() if not (k.endswith('_last_attempt') and isinstance(v, dict) and v.get('batch_id') == key)}
            atomic_json(document_path, document)
        with self.connection(write=True) as connection:
            for item in manifest:
                if item['asset_id']:
                    # 引用已在上一事务检查并删除；并发新增引用由外键RESTRICT阻止误删。
                    connection.execute(assets.delete().where(assets.c.id == item['asset_id']))
            counts['files'] = deleted
            connection.execute(discarded.update().where(discarded.c.source_batch_key == key).values(stage='complete', counts=counts, updated_at=database_now()))
        return counts
