"""只读核对最近详情入口，不把商品的历史搜索词当作实际入口。"""
import sqlalchemy as sa


def attach_detail_entries(connection,tables,rows):
    """补充本页商品入口说明；参数：连接、表集合、商品行；返回：原位附加展示字段。"""
    if not rows: return
    e,t,l,b=[tables[name] for name in ('search_records','tasks','list_observations','collection_batches')]
    identifiers=[row['id'] for row in rows]
    ranked=sa.select(e,t.c.name.label('task_name'),sa.func.row_number().over(partition_by=e.c.product_id,
        order_by=[e.c.captured_at.desc(),e.c.id.desc()]).label('rank')).outerjoin(t,t.c.id==e.c.task_id).where(
        e.c.product_id.in_(identifiers),e.c.record_type=='detail_open').subquery('catalog_entry_ranked')
    latest={entry['product_id']:dict(entry) for entry in connection.execute(sa.select(ranked).where(ranked.c.rank==1)).mappings()}
    entries={entry['id']:entry for entry in latest.values()}
    missing={row['run_entry_record_id'] for row in rows if row.get('run_entry_record_id') is not None}-entries.keys()
    if missing:
        entries.update({entry['id']:dict(entry) for entry in connection.execute(sa.select(e,t.c.name.label('task_name')).outerjoin(
            t,t.c.id==e.c.task_id).where(e.c.id.in_(missing))).mappings()})
    sources={entry['source_list_observation_id'] for entry in entries.values() if entry['source_list_observation_id'] is not None}
    listing={}
    if sources:
        listing={item['id']:dict(item) for item in connection.execute(sa.select(l.c.id,l.c.product_id,b.c.task_id,
            b.c.search_keyword).join(b,b.c.id==l.c.batch_id).where(l.c.id.in_(sources),b.c.visibility=='published',b.c.module=='list')).mappings()}
    for row in rows:
        newest=latest.get(row['id'])
        # 较新的运行没有入口关联时，不借用较早入口；运行后另有新入口则按新入口展示。
        entry=newest
        if row.get('run_id') is not None:
            entry=entries.get(row.get('run_entry_record_id'))
            if newest and newest['captured_at'] is not None and row['run_started_at'] is not None and newest['captured_at']>row['run_started_at']:
                entry=newest
            elif entry and (entry['product_id']!=row['id'] or not entry['task_id'] or entry['task_id']!=row['run_task_id']):
                entry=None
        _classify(row,entry,listing)


def _classify(row,entry,listing):
    """按明确记录分类；参数：商品、所选入口、已发布列表来源；返回：原位补充，不推断缺失信息。"""
    kind='not_started' if row['origin'] in {'list','registered'} else 'unknown'
    row.update(detail_entry_kind=kind,detail_entry_label='尚无详情记录' if kind=='not_started' else '入口未记录',
        detail_entry_id=None,detail_entry_at=None,detail_entry_keyword=None)
    if not entry or entry['record_type']!='detail_open' or entry['product_id']!=row['id'] or not entry['task_id']:
        return
    row.update(detail_entry_id=entry['id'],detail_entry_at=entry['captured_at'])
    pointer=entry['source_list_observation_id']
    if pointer is not None:
        source=listing.get(pointer)
        if source and source['product_id']==row['id'] and source['task_id']==entry['task_id'] and (
                not entry['keyword'] or entry['keyword']==source['search_keyword']):
            row.update(detail_entry_kind='search',detail_entry_label='搜索列表进入',detail_entry_keyword=source['search_keyword'])
        return
    payload=entry['payload'] if isinstance(entry['payload'],dict) else {}
    record=payload.get('record') if isinstance(payload.get('record'),dict) else {}
    # NULL关联也可能是旧来源未知；仅独立任务及原始输入身份均明确时才标记直接采集。
    if (entry['task_name']=='collection.taobao.detail.open' and record.get('input_type') in {'id','url'} and
            record.get('task_id')==entry['task_id'] and record.get('product_id')==row['platform_product_id'] and
            record.get('list_source') is None and not entry['keyword'] and not entry.get('search_method')):
        row.update(detail_entry_kind='direct',detail_entry_label='直接采集')
