"""0007冻结的数据转换规则：从实际保存的旧SKU信息派生独立观察，允许幂等补写。"""
from datetime import datetime,timedelta,timezone
import hashlib
import json
from urllib.parse import urlsplit,parse_qs
import sqlalchemy as sa


def digest(value):
    """计算冻结范围/来源键；参数：JSON值；返回：SHA256。"""
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def split_legacy_info(connection,tables,*,batch_id=None):
    """提取已发布旧信息；参数：同事务连接、表及可选旧批次；返回：新增、跳过和来源编号。"""
    b,info_table,current=[tables[n] for n in ('collection_batches','product_info_observations','product_current_sections')]
    query=sa.select(b).where(b.c.module=='skus',b.c.visibility=='published',b.c.product_id.is_not(None))
    if batch_id is not None:
        query=query.where(b.c.id==batch_id)
    result={'created':0,'skipped':0,'sources':[]}
    for source in connection.execute(query.order_by(b.c.captured_at,b.c.id)).mappings().all():
        record=(source['result_payload'] or {}).get('record') or {}
        info=record.get('product_info')
        if not isinstance(info,dict) or not any(info.get(key) for key in ('title','sales_text','shop_name','price_text')):
            result['skipped']+=1
            continue
        key='basic-from-sku-'+digest([source['source_batch_key'],info])
        existing=connection.execute(sa.select(b).where(b.c.platform==source['platform'],b.c.module=='basic_info',b.c.source_batch_key==key)).mappings().first()
        if existing:
            result['skipped']+=1
            continue
        values={k:info.get(k) if isinstance(info.get(k),str) and info[k].strip() else None for k in ('title','shop_name','sales_text','price_text')}
        states={k:'provided' if value else 'not_recorded' for k,value in values.items()}
        captured=None
        raw_time=info.get('captured_at')
        try:
            if raw_time:
                time=datetime.fromisoformat(raw_time)
                if time.tzinfo is not None:
                    captured=time.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
            elif source['captured_at'] is not None:
                captured=source['captured_at']
        except (ValueError,TypeError):
            pass
        source_url=source['source_url']
        platform_id=connection.scalar(sa.select(tables['products'].c.platform_product_id).where(tables['products'].c.id==source['product_id']))
        try:
            url=urlsplit(source_url or '')
            source_ok=(url.scheme in {'http','https'} and url.hostname in {'item.taobao.com','detail.tmall.com'}
                       and url.path=='/item.htm' and parse_qs(url.query).get('id')==[platform_id])
        except ValueError:
            source_ok=False
        complete=bool(values['title'] and captured and source_ok)
        status='complete' if complete else 'partial'
        time_text=captured.replace(tzinfo=timezone(timedelta(hours=8))).isoformat(timespec='microseconds') if captured else None
        data={**info,**values,'field_states':states,'captured_at':time_text}
        contract={'version':1,'module':'basic_info','scope':'product_basic_info',**{k:{'missing':True} for k in
                  ('mode','requested_limit','answer_limit','filter','sort','keyword','page_number','sort_label')}}
        scope_key=digest(contract)
        derived_record={'schema_version':1,'product_id':platform_id,'task_id':source['task_id'],'batch_id':key,
            'source_url':source_url,'scope':'product_basic_info','captured_at':time_text,'status':status,'product_info':data,
            'migration_source':{'kind':'sku_basic_info_split','batch_id':source['id'],'source_batch_key':source['source_batch_key'],
                                'time_source':'product_info' if raw_time else 'batch_metadata'}}
        payload={'record':derived_record,'scope_contract':contract,'_migration':'0007_basic_info',
                 '_last_attempt_selected':False}
        if complete:
            payload.update(_current_sections=['product_info'],_export_sections=[])
        root={k:v for k,v in source.items() if k!='id'}
        root.update(source_batch_key=key,module='basic_info',status=status,step_id=None,
                    scope='product_basic_info',scope_key=scope_key,collection_mode=None,requested_limit=None,answer_limit=None,
                    selection_filter=None,sort_label=None,coverage_status='unknown' if complete else 'incomplete',
                    stop_reason=None,end_confirmed=None,completion_evidence=None,captured_at=captured,observed_at=captured,confirmed_at=None,
                    derived_from_batch_id=source['id'],upstream_batch_key=source['source_batch_key'],derivation_kind='sku_basic_info_split',
                    schema_version=1,summary={'source_sku_batch_id':source['id'],'missing_fields':[k for k,v in values.items() if v is None]},
                    result_payload=payload)
        new_id=connection.execute(b.insert().values(**root)).inserted_primary_key[0]
        connection.execute(info_table.insert().values(batch_id=new_id,product_id=source['product_id'],module='basic_info',
                    **values,source_url=source_url,captured_at=captured,field_states=states,payload=data))
        # 保留旧运行八步与SKU原文，仅独立更新基本信息的有效分区。
        if complete:
            pointer=connection.execute(sa.select(current).where(current.c.product_id==source['product_id'],current.c.section=='product_info',
                                                                current.c.module=='basic_info',current.c.scope_key==scope_key)).mappings().first()
            previous=connection.execute(sa.select(b).where(b.c.id==pointer['batch_id'])).mappings().first() if pointer else None
            if not previous or (captured,new_id)>((previous['captured_at'] or datetime.min),previous['id']):
                if pointer:
                    old=dict(previous['result_payload'] or {})
                    old['_export_sections']=[]
                    connection.execute(b.update().where(b.c.id==previous['id']).values(result_payload=old))
                    connection.execute(current.delete().where(current.c.product_id==source['product_id'],current.c.section=='product_info',current.c.scope_key==scope_key))
                connection.execute(current.insert().values(product_id=source['product_id'],section='product_info',module='basic_info',
                        scope_key=scope_key,batch_id=new_id,published_at=captured,publication_basis='legacy_basic_info_independently_checked'))
                payload['_export_sections']=['product_info']
                connection.execute(b.update().where(b.c.id==new_id).values(result_payload=payload))
        result['created']+=1
        result['sources'].append({'source_batch_id':source['id'],'batch_id':new_id,'product_id':source['product_id'],'status':status})
    return result
