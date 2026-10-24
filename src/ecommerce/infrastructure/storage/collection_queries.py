"""CLI/Web 共用的采集历史查询与执行期有效分区读取。"""
from datetime import datetime
from decimal import Decimal
import copy
import re

import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_store import collection_store,SECTIONS
from ecommerce.infrastructure.storage.mysql import MySQLStorageError,safe_error
from ecommerce.infrastructure.storage.task_store import iso_value
from ecommerce.infrastructure.tasks.events import sanitize

RECORD_KINDS={'items':'list_observations','skus':'sku_observations','parameters':'parameter_observations',
    'reviews':'review_observations','questions':'question_observations','answers':'answer_observations',
    'media':'media_references','checkpoints':'collection_checkpoints','review_summary':'review_summaries'}
DEFAULT_KIND={'list':'items','skus':'skus','parameters':'parameters','reviews':'reviews','questions':'questions',
              'review_summary':'review_summary','video':'media','main_images':'media','detail_images':'media'}


def _page(after,limit):
    """校验分页；参数：游标和数量；返回：无。"""
    if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=200:
        raise ValueError('采集查询游标须非负，每页数量为 1–200')


def public(value):
    """序列化查询结果并隐藏内部控制键；参数：任意结果；返回：安全 JSON 值。"""
    if isinstance(value,dict):
        return sanitize({str(k):public(v) for k,v in value.items() if not str(k).startswith('_')})
    if isinstance(value,(list,tuple)):
        return [public(v) for v in value]
    if isinstance(value,datetime):
        return iso_value(value)
    if isinstance(value,Decimal):
        return str(value)
    return value


def _visible(store,table,*,quarantined=False,own_task=None):
    """限定已发布或当前任务的工作记录；参数：存储、表、查询范围；返回：过滤条件。"""
    states=['published','quarantined'] if quarantined else ['published']
    visibility=table.c.visibility.in_(states)
    if own_task:
        visibility=sa.or_(visibility,sa.and_(table.c.task_id==own_task,table.c.visibility=='staging'))
    return visibility


def products(settings,*,after=0,limit=50,keyword=None):
    """列出有正式来源的商品；参数：配置、分页、标题或编号；返回：商品与来源计数。"""
    _page(after,limit)
    if keyword is not None and (not isinstance(keyword,str) or len(keyword)>200):
        raise ValueError('查询关键词最长 200 字符')
    store=collection_store(settings)
    p,b,l,s=map(store.tables.__getitem__,['products','collection_batches','list_observations','search_records'])
    batch_exists=sa.exists(sa.select(b.c.id).where(b.c.product_id==p.c.id,_visible(store,b)))
    list_exists=sa.exists(sa.select(l.c.id).join(b,b.c.id==l.c.batch_id).where(l.c.product_id==p.c.id,_visible(store,b)))
    page_exists=sa.exists(sa.select(s.c.id).where(s.c.product_id==p.c.id))
    statement=sa.select(p).where(p.c.id>after,sa.or_(batch_exists,list_exists,page_exists)).order_by(p.c.id).limit(limit)
    if keyword:
        statement=statement.where(sa.or_(p.c.platform_product_id==keyword,p.c.display_title.contains(keyword,autoescape=True)))
    try:
        with store.engine.connect() as connection:
            rows=[dict(r) for r in connection.execute(statement).mappings()]
            for item in rows:
                item['detail_batch_count']=connection.scalar(sa.select(sa.func.count()).select_from(b).where(b.c.product_id==item['id'],_visible(store,b)))
                item['detail_entry_count']=connection.scalar(sa.select(sa.func.count()).select_from(s).where(s.c.product_id==item['id'],s.c.record_type=='detail_open'))
        return {'products':public(rows),'next_cursor':rows[-1]['id'] if rows else after}
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def batches(settings,*,after=0,limit=50,product_id=None,module=None,include_quarantined=False):
    """列出实际采集批次；参数：配置、分页、原商品编号、模块及隔离开关；返回：范围与状态。"""
    _page(after,limit)
    store=collection_store(settings)
    b,p=store.tables['collection_batches'],store.tables['products']
    if module and module not in DEFAULT_KIND:
        raise ValueError('不支持的采集模块')
    statement=sa.select(b,p.c.platform_product_id).outerjoin(p,p.c.id==b.c.product_id).where(b.c.id>after,
        _visible(store,b,quarantined=include_quarantined)).order_by(b.c.id).limit(limit)
    if product_id:
        if not isinstance(product_id,str) or not re.fullmatch(r'[0-9]{1,25}',product_id) or int(product_id)==0:
            raise ValueError('商品编号格式无效')
        statement=statement.where(p.c.platform_product_id==product_id)
    if module:
        statement=statement.where(b.c.module==module)
    try:
        with store.engine.connect() as connection:
            rows=[dict(row) for row in connection.execute(statement).mappings()]
        for item in rows:
            item.pop('result_payload',None)
        return {'batches':public(rows),'next_cursor':rows[-1]['id'] if rows else after}
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def records(settings,batch_id,*,kind=None,after=0,limit=100,question_id=None,include_quarantined=False):
    """查询单批次明细；参数：内部批次、明细种类、分页及可选问题；返回：原范围和观察。"""
    _page(after,limit)
    if type(batch_id) is not int or batch_id<=0 or (question_id is not None and (type(question_id) is not int or question_id<=0)):
        raise ValueError('批次及问题编号须为正整数')
    store=collection_store(settings)
    b=store.tables['collection_batches']
    try:
        with store.engine.connect() as connection:
            batch=connection.execute(sa.select(b).where(b.c.id==batch_id,_visible(store,b,quarantined=include_quarantined))).mappings().one_or_none()
            if batch is None:
                raise LookupError('批次不存在或尚未发布')
            kind=kind or DEFAULT_KIND[batch['module']]
            if kind not in RECORD_KINDS:
                raise ValueError('不支持的采集明细种类')
            table=store.tables[RECORD_KINDS[kind]]
            if kind not in {'answers','media','checkpoints'} and DEFAULT_KIND[batch['module']]!=kind:
                raise ValueError('明细种类与批次模块不一致')
            if kind=='answers' and batch['module']!='questions':
                raise ValueError('回答只属于问答批次')
            cursor=table.c.id if 'id' in table.c else table.c.batch_id
            statement=sa.select(table).where(table.c.batch_id==batch_id,cursor>after).order_by(cursor).limit(limit)
            if question_id:
                if kind!='answers':
                    raise ValueError('问题筛选仅用于回答')
                statement=statement.where(table.c.question_id==question_id)
            rows=[dict(row) for row in connection.execute(statement).mappings()]
            if kind=='media':
                file_table=store.tables['file_assets']
                for item in rows:
                    item['file']=dict(connection.execute(sa.select(file_table).where(file_table.c.id==item['file_asset_id'])).mappings().one()) if item.get('file_asset_id') else None
        batch=dict(batch)
        batch.pop('result_payload',None)
        return {'batch':public(batch),'kind':kind,'records':public(rows),
                'next_cursor':rows[-1]['id' if 'id' in table.c else 'batch_id'] if rows else after}
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def detail_document(settings,item_id,*,own_task=None):
    """重建执行期分区；参数：原商品编号、可选当前任务；返回：兼容原文件的分区字典。"""
    store=collection_store(settings)
    b,p=store.tables['collection_batches'],store.tables['products']
    try:
        with store.engine.connect() as connection:
            rows=connection.execute(sa.select(b).join(p,p.c.id==b.c.product_id).where(p.c.platform_product_id==item_id,
                _visible(store,b,own_task=own_task)).order_by(b.c.id)).mappings().all()
            result={'schema_version':2,'product_id':item_id}
            for batch in rows:
                payload=batch['result_payload'] or {}
                sections=payload.get('_export_sections',[])
                if sections:
                    current=store.tables['product_current_sections']
                    registered=set(connection.execute(sa.select(current.c.section).where(current.c.batch_id==batch['id'])).scalars())
                    if not set(sections)<=registered:
                        raise MySQLStorageError('当前采集分区与数据库指针不一致，请先核对')
                attempt=payload.get('_last_attempt_selected') is True
                if not sections and not attempt:
                    continue
                record=copy.deepcopy(payload.get('record') or {})
                module=batch['module']
                if module=='skus':
                    table=store.tables['sku_observations']
                    skus=[]
                    for value in connection.execute(sa.select(table).where(table.c.batch_id==batch['id']).order_by(table.c.position)).mappings():
                        sku={k:v for k,v in (value['payload'] or {}).items() if not k.startswith('_')}
                        media=store.tables['media_references']
                        image=connection.scalar(sa.select(media.c.payload).where(media.c.batch_id==batch['id'],media.c.sku_observation_id==value['id'],media.c.role=='sku_image'))
                        sku['image']=(image or {}).get('record')
                        skus.append(sku)
                    record['skus']=skus
                elif module in {'main_images','detail_images'}:
                    table=store.tables['media_references']
                    role='main_image' if module=='main_images' else 'detail_image'
                    record['images']=[r['record'] for r in connection.execute(sa.select(table.c.payload).where(
                        table.c.batch_id==batch['id'],table.c.role==role).order_by(table.c.position)).scalars()]
                elif module=='parameters':
                    table=store.tables['parameter_observations']
                    record['items']=[{k:v for k,v in raw.items() if not k.startswith('_')} for raw in connection.execute(
                        sa.select(table.c.payload).where(table.c.batch_id==batch['id']).order_by(table.c.position)).scalars()]
                for section in sections:
                    result[section]=record.get('product_info',{}) if section=='product_info' else record
                if attempt:
                    name='sku_details' if module=='skus' else module
                    result[name+'_last_attempt']=record
            runs,steps=store.tables['collection_runs'],store.tables['collection_steps']
            latest=connection.execute(sa.select(runs).join(p,p.c.id==runs.c.product_id).where(
                p.c.platform_product_id==item_id).order_by(runs.c.started_at.desc(),runs.c.id.desc()).limit(1)).mappings().first()
            if latest:
                saved_steps=list(connection.execute(sa.select(steps).where(steps.c.run_id==latest['id']).order_by(steps.c.id)).mappings())
                metadata=next(((step['payload'] or {}).get('run_metadata') for step in saved_steps if (step['payload'] or {}).get('run_metadata')),None)
                result['collection_summary']={**(metadata or {}),'product_id':item_id,'task_id':latest['task_id'],
                    'batch_id':latest['source_run_key'],'status':latest['status'],'started_at':iso_value(latest['started_at']),
                    'finished_at':iso_value(latest['finished_at']),'warnings':latest['warnings'],'error':latest['error'],
                    'steps':[(step['payload'] or {}).get('record') or {} for step in saved_steps]}
            return result
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None
