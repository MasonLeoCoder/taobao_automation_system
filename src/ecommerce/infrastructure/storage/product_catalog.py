"""商品库的只读查询：全部已登记淘宝商品及各自真实来源。"""
import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_context import DETAIL_MODULES, RUN_LABELS
from ecommerce.infrastructure.storage.pagination import numbered_rows
from ecommerce.infrastructure.storage.sorting import sorted_statement
from ecommerce.shared.sales import sales_filters


def product_catalog(connection,tables,options,date_bounds):
    """分页查询商品库；参数：连接、表、筛选、日期边界；返回：商品及可追溯摘要。"""
    from ecommerce.infrastructure.storage.collection_browser import _text
    p,b,l,info,r,e,cur=[tables[name] for name in ('products','collection_batches','list_observations',
        'product_info_observations','collection_runs','search_records','product_current_sections')]
    visible=b.c.visibility=='published'
    listed=sa.select(l,b.c.captured_at.label('list_captured_at'),
        sa.func.row_number().over(partition_by=l.c.product_id,order_by=[b.c.captured_at.desc(),b.c.id.desc(),l.c.id.desc()]).label('rank'))
    listed=listed.join(b,b.c.id==l.c.batch_id).where(visible,b.c.module=='list',l.c.product_id.is_not(None)).subquery('catalog_list_ranked')
    latest_list=sa.select(listed).where(listed.c.rank==1).subquery('catalog_latest_list')
    basic=sa.select(info,b.c.captured_at.label('batch_captured_at'),
        sa.func.row_number().over(partition_by=info.c.product_id,order_by=[b.c.captured_at.desc(),b.c.id.desc()]).label('rank'))
    basic=basic.join(b,sa.and_(b.c.id==info.c.batch_id,b.c.product_id==info.c.product_id)).where(
        visible,b.c.module=='basic_info',b.c.status=='complete').subquery('catalog_info_ranked')
    latest_info=sa.select(basic).where(basic.c.rank==1).subquery('catalog_latest_info')
    valid=sa.select(cur.c.product_id,sa.func.count(sa.distinct(cur.c.module)).label('valid_count')).join(
        b,sa.and_(b.c.id==cur.c.batch_id,b.c.product_id==cur.c.product_id,b.c.module==cur.c.module)).where(
        visible,b.c.module.in_(DETAIL_MODULES),b.c.status.in_(['complete','downloaded','no_video','no_images',
        'no_parameters','no_reviews','no_questions'])).group_by(cur.c.product_id).subquery('catalog_valid')
    # 各来源独立成为事件，按实际时间取最大值，不以COALESCE优先级替代时间比较。
    events=sa.union_all(
        sa.select(l.c.product_id,b.c.captured_at.label('recorded_at'),sa.literal('列表采集').label('event_label'),
                  b.c.id.label('event_id'),sa.literal(1).label('priority')).join(b,b.c.id==l.c.batch_id).where(visible,b.c.module=='list'),
        sa.select(b.c.product_id,b.c.captured_at,sa.literal('详情资料保存'),b.c.id,sa.literal(2)).where(visible,b.c.module.in_(DETAIL_MODULES)),
        sa.select(r.c.product_id,r.c.started_at,sa.literal('详情采集尝试'),r.c.id,sa.literal(3)),
        sa.select(e.c.product_id,e.c.captured_at,sa.literal('详情访问'),e.c.id,sa.literal(0)).where(e.c.record_type=='detail_open')
    ).subquery('catalog_events')
    ranked_events=sa.select(events,sa.func.row_number().over(partition_by=events.c.product_id,
        order_by=[events.c.recorded_at.desc(),events.c.priority.desc(),events.c.event_id.desc()]).label('rank')).subquery('catalog_ranked_events')
    latest_event=sa.select(ranked_events).where(ranked_events.c.rank==1).subquery('catalog_latest_event')
    run_ranked=sa.select(r.c.id,r.c.product_id,r.c.status,r.c.started_at,r.c.task_id,r.c.entry_record_id,
        sa.func.row_number().over(partition_by=r.c.product_id,order_by=[r.c.started_at.desc(),r.c.id.desc()]).label('rank')).subquery('catalog_run_ranked')
    latest_run=sa.select(run_ranked).where(run_ranked.c.rank==1).subquery('catalog_latest_run')
    has_list=latest_list.c.id.is_not(None)
    detail_activity=sa.or_(
        sa.exists(sa.select(b.c.id).where(b.c.product_id==p.c.id,visible,b.c.module.in_(DETAIL_MODULES))),
        sa.exists(sa.select(e.c.id).where(e.c.product_id==p.c.id,e.c.record_type=='detail_open')),
        sa.exists(sa.select(r.c.id).where(r.c.product_id==p.c.id)))
    valid_count=sa.func.coalesce(valid.c.valid_count,0)
    state=sa.case((valid_count==9,'complete'),(valid_count>0,'partial'),(detail_activity,'attempt'),(has_list,'list'),else_='registered')
    origin=sa.case((sa.and_(has_list,detail_activity),'both'),(has_list,'list'),(detail_activity,'detail'),else_='registered')
    chosen=lambda name:sa.case((has_list,latest_list.c[name]),else_=latest_info.c[name])
    statement=sa.select(p,chosen('price_text').label('price_text'),chosen('sales_text').label('sales_text'),
        chosen('shop_name').label('shop_name'),latest_list.c.location_text,latest_list.c.price_value,latest_list.c.sales_count,
        latest_list.c.sales_precision,latest_list.c.ad_status,latest_list.c.id.label('list_observation_id'),
        latest_list.c.batch_id.label('list_batch_id'),latest_list.c.slot_index.label('list_slot_index'),
        latest_info.c.batch_id.label('info_batch_id'),
        sa.case((has_list,'列表页'),(latest_info.c.id.is_not(None),'详情页'),else_=None).label('value_source'),
        sa.case((has_list,latest_list.c.list_captured_at),else_=latest_info.c.captured_at).label('value_captured_at'),
        valid_count.label('valid_count'),state.label('catalog_state'),origin.label('origin'),
        latest_event.c.recorded_at.label('captured_at'),latest_event.c.event_label,
        latest_run.c.id.label('run_id'),latest_run.c.entry_record_id.label('run_entry_record_id'),
        latest_run.c.status.label('run_status'),latest_run.c.started_at.label('run_started_at'),latest_run.c.task_id.label('run_task_id'))
    statement=statement.select_from(p).outerjoin(latest_list,latest_list.c.product_id==p.c.id).outerjoin(latest_info,latest_info.c.product_id==p.c.id)
    statement=statement.outerjoin(valid,valid.c.product_id==p.c.id).outerjoin(latest_event,latest_event.c.product_id==p.c.id)
    statement=statement.outerjoin(latest_run,latest_run.c.product_id==p.c.id).where(p.c.platform=='taobao')
    keyword,shop,source=[_text(options.get(key,'')) for key in ('keyword','shop','source')]
    if keyword:
        statement=statement.where(sa.or_(p.c.platform_product_id==keyword,p.c.display_title.contains(keyword,autoescape=True)))
    if shop:
        statement=statement.where(chosen('shop_name').contains(shop,autoescape=True))
    if source:
        statement=statement.where(sa.exists(sa.select(l.c.id).join(b,b.c.id==l.c.batch_id).where(
            l.c.product_id==p.c.id,visible,b.c.module=='list',b.c.search_keyword==source)))
    selected_origin=options.get('origin','')
    if selected_origin:
        if selected_origin not in {'list','detail','both','registered'}: raise ValueError('商品来源筛选无效')
        statement=statement.where(origin==selected_origin)
    selected_state=options.get('state','')
    if selected_state=='effective':
        statement=statement.where(valid_count>0)
    elif selected_state:
        if selected_state not in {'complete','partial','attempt','list','registered'}: raise ValueError('资料情况筛选无效')
        statement=statement.where(state==selected_state)
    for key,value in date_bounds.items():
        statement=statement.where(latest_event.c.recorded_at>=value if key=='since' else latest_event.c.recorded_at<value)
    sales=sales_filters(options)
    if sales['sales_min'] is not None: statement=statement.where(latest_list.c.sales_count>=sales['sales_min'])
    if sales['sales_max'] is not None: statement=statement.where(latest_list.c.sales_count<=sales['sales_max'])
    columns={'title':p.c.display_title,'price':latest_list.c.price_value,'sales':latest_list.c.sales_count,
        'shop':chosen('shop_name'),'captured_at':latest_event.c.recorded_at,'modules':valid_count}
    ordered,spec=sorted_statement(statement,columns,p.c.id,options.get('sort_by'),options.get('order'),'captured_at','desc')
    rows,page_info=numbered_rows(connection,ordered,options.get('page',1))
    _decorate(connection,tables,rows)
    return {'products':rows,**page_info,'sort_spec':spec}


def _decorate(connection,tables,rows):
    """补充当前页来源和候选缩略图；参数：连接、表、商品；返回：无，原位附加。"""
    if not rows: return
    from ecommerce.infrastructure.storage.list_image_preview import thumbnail
    b,l,m,f=[tables[name] for name in ('collection_batches','list_observations','media_references','file_assets')]
    by_id={row['id']:row for row in rows}
    from ecommerce.infrastructure.storage.detail_entry_source import attach_detail_entries
    attach_detail_entries(connection,tables,rows)
    for row in rows:
        row.update(search_keywords=[],thumbnail_candidates=[],thumbnail=None,thumbnail_file=None,thumbnail_message='暂无缩略图',
            remaining_count=9-row['valid_count'],run_label=RUN_LABELS.get(row['run_status'],row['run_status'] or '未记录详情运行'))
    sources=connection.execute(sa.select(l.c.product_id,sa.func.nullif(b.c.search_keyword,'')).join(b,b.c.id==l.c.batch_id).where(
        l.c.product_id.in_(by_id),b.c.visibility=='published',b.c.module=='list').distinct().order_by(sa.func.nullif(b.c.search_keyword,''))).all()
    for product_id,keyword in sources:
        by_id[product_id]['search_keywords'].append(keyword)
    images=sa.select(b.c.product_id,f,
        sa.func.row_number().over(partition_by=b.c.product_id,order_by=[b.c.captured_at.desc(),b.c.id.desc(),m.c.position,m.c.id]).label('rank'))
    images=images.select_from(m.join(b,b.c.id==m.c.batch_id).join(f,f.c.id==m.c.file_asset_id)).where(
        b.c.product_id.in_(by_id),b.c.visibility=='published',b.c.module=='main_images',m.c.role=='main_image',
        m.c.download_status.in_(['downloaded','reused']),f.c.availability=='present').subquery('catalog_images')
    for image in connection.execute(sa.select(images).where(images.c.rank==1)).mappings():
        asset={key:image[key] for key in f.c.keys()}
        by_id[image['product_id']]['thumbnail_candidates'].append({'thumbnail':asset['id'],'thumbnail_file':asset,'thumbnail_message':'本地图片不可用'})
    for row in rows:
        if row['list_observation_id'] is not None:
            preview={'batch_id':row['list_batch_id'],'slot_index':row['list_slot_index']}
            thumbnail(connection,tables,preview)
            row['thumbnail_candidates'].append(preview)
