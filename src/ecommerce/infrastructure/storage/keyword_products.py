"""搜索词商品浏览：同一次列表观察成行，按范围内最新观察汇总。"""
import sqlalchemy as sa
from ecommerce.infrastructure.storage.collection_context import detail_summary,observation_context
from ecommerce.infrastructure.storage.pagination import numbered_rows
from ecommerce.infrastructure.storage.sorting import sorted_statement


def keyword_products(connection,tables,options,date_bounds):
    """读取搜索词商品及历史；参数：连接、表、筛选、日期边界；返回：分页观察和资料状态。"""
    from ecommerce.infrastructure.storage.collection_browser import _text
    b,l,p,cur=[tables[k] for k in ('collection_batches','list_observations','products','product_current_sections')]
    mode=options.get('mode','summary')
    if mode not in {'summary','records'}: raise ValueError('商品视图无效')
    category=options.get('category')
    if category is None and not options.get('category_missing'): raise ValueError('请先选择搜索词')
    conditions=[b.c.visibility=='published',b.c.module=='list']
    conditions.append(sa.func.nullif(b.c.search_keyword,'').is_(None) if options.get('category_missing') else b.c.search_keyword==_text(category))
    if 'since' in date_bounds: conditions.append(b.c.captured_at>=date_bounds['since'])
    if 'until' in date_bounds: conditions.append(b.c.captured_at<date_bounds['until'])
    partition=[l.c.product_id,sa.case((l.c.product_id.is_(None),l.c.id),else_=0)]
    base=sa.select(l,p.c.platform_product_id,b.c.task_id,b.c.search_keyword,b.c.page_number,b.c.captured_at,
        sa.func.row_number().over(partition_by=partition,order_by=[b.c.captured_at.desc(),b.c.id.desc(),l.c.id.desc()]).label('latest_rank'),
        sa.func.count().over(partition_by=partition).label('occurrences')).join(b,b.c.id==l.c.batch_id).outerjoin(p,p.c.id==l.c.product_id).where(*conditions)
    observed=base.subquery('keyword_observations')
    scope_records=connection.scalar(sa.select(sa.func.count()).select_from(observed))
    scope_products=connection.scalar(sa.select(sa.func.count()).select_from(observed).where(observed.c.latest_rank==1))
    statement=sa.select(observed)
    if mode=='summary': statement=statement.where(observed.c.latest_rank==1)
    keyword,shop,product=[_text(options.get(key,'')) for key in ('keyword','shop','product')]
    if keyword:
        statement=statement.where(sa.or_(observed.c.platform_product_id==keyword,observed.c.title.contains(keyword,autoescape=True)))
    if shop: statement=statement.where(observed.c.shop_name.contains(shop,autoescape=True))
    if product: statement=statement.where(observed.c.platform_product_id==product)
    from ecommerce.shared.sales import sales_filters
    sales=sales_filters(options)
    if sales['sales_min'] is not None: statement=statement.where(observed.c.sales_count>=sales['sales_min'])
    if sales['sales_max'] is not None: statement=statement.where(observed.c.sales_count<=sales['sales_max'])
    for name in ('sales_metric','sales_period','sales_precision'):
        if sales[name]: statement=statement.where(observed.c[name]==sales[name])
    ad_status=options.get('ad_status','')
    if ad_status in {'identified','not_identified'}:
        statement=statement.where(observed.c.ad_status==ad_status)
    elif ad_status=='unconfirmed':
        statement=statement.where(sa.or_(observed.c.ad_status=='unconfirmed',observed.c.ad_status.is_(None)))
    elif ad_status:
        raise ValueError('广告识别筛选无效')
    state=options.get('state','')
    content=sa.exists(sa.select(b.c.id).where(b.c.product_id==observed.c.product_id,b.c.module!='list',b.c.visibility=='published'))
    effective=sa.exists(sa.select(cur.c.batch_id).join(b,b.c.id==cur.c.batch_id).where(
        cur.c.product_id==observed.c.product_id,b.c.visibility=='published'))
    if state=='effective': statement=statement.where(effective)
    elif state=='attempt': statement=statement.where(content,~effective)
    elif state=='none': statement=statement.where(~content)
    elif state: raise ValueError('详情资料筛选无效')
    columns={'captured_at':observed.c.captured_at,'title':observed.c.title,'price':observed.c.price_value,'sales':observed.c.sales_count,
        'shop':observed.c.shop_name,'position':observed.c.slot_index,'occurrences':observed.c.occurrences}
    ordered,spec=sorted_statement(statement,columns,observed.c.id,options.get('sort_by'),options.get('order'),'captured_at','desc')
    rows,info=numbered_rows(connection,ordered,options.get('page',1))
    summaries={}
    for row in rows:
        if row['product_id'] and row['product_id'] not in summaries:
            summaries[row['product_id']]=detail_summary(connection,tables,row['product_id'])
        row['detail_context']=observation_context(connection,tables,row,summaries.get(row['product_id']))
        from ecommerce.infrastructure.storage.list_image_preview import thumbnail
        thumbnail(connection,tables,row)
        preceding=sa.or_(l.c.slot_index<row['slot_index'],sa.and_(l.c.slot_index==row['slot_index'],l.c.id<row['id'])) if row['slot_index'] is not None else sa.or_(l.c.slot_index.is_not(None),sa.and_(l.c.slot_index.is_(None),l.c.id<row['id']))
        count_before=connection.scalar(sa.select(sa.func.count()).select_from(l).where(l.c.batch_id==row['batch_id'],preceding))
        from ecommerce.shared.pagination import PAGE_SIZE
        row['source_page']=count_before//PAGE_SIZE+1
    latest=connection.scalar(sa.select(sa.func.max(observed.c.captured_at)))
    return {'observations':rows,**info,'sort_spec':spec,'mode':mode,'scope_records':scope_records,
        'scope_products':scope_products,'latest_at':latest}
