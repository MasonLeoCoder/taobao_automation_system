"""同任务列表批次融合查询：保留每次出现的原始来源，统一排序分页。"""
import sqlalchemy as sa
from ecommerce.infrastructure.storage.pagination import numbered_rows
from ecommerce.infrastructure.storage.sorting import sorted_statement
from ecommerce.infrastructure.storage.collection_context import detail_summary, observation_context
from ecommerce.infrastructure.storage.list_image_preview import thumbnail


def search_observations(connection, tables, batches, options):
    """查询获准范围的列表观察；参数：连接、表、已核对批次、筛选；返回：记录、统计和分页。"""
    b,l,p=[tables[name] for name in ('collection_batches','list_observations','products')]
    allowed={row['id']:row for row in batches}
    selected=options.get('batch_id',0)
    if selected and selected not in allowed:
        raise LookupError('批次不属于本次搜索或日期范围')
    ids=[selected] if selected else list(allowed)
    summary=connection.execute(sa.select(sa.func.count(l.c.id).label('record_count'),
        sa.func.count(sa.distinct(l.c.product_id)).label('product_count'),
        sa.func.count(sa.case((l.c.product_id.is_(None),1))).label('unknown_count')).where(l.c.batch_id.in_(ids))).mappings().one()
    scope_batches=[allowed[identifier] for identifier in ids]
    summary=dict(summary)
    summary.update(batch_count=len(scope_batches),
        search_page_count=len({(row['search_keyword'],row['page_number']) for row in scope_batches if row['page_number'] is not None}),
        unknown_page_batches=sum(row['page_number'] is None for row in scope_batches))
    counts=dict(connection.execute(sa.select(l.c.batch_id,sa.func.count()).where(l.c.batch_id.in_(list(allowed))).group_by(l.c.batch_id)).all())
    for row in batches:
        row['record_count']=counts.get(row['id'],0)
    statement=sa.select(l,p.c.platform_product_id,b.c.captured_at,b.c.search_keyword,b.c.page_number,
                        b.c.status.label('batch_status')).join(b,b.c.id==l.c.batch_id).outerjoin(p,p.c.id==l.c.product_id).where(l.c.batch_id.in_(ids))
    columns={'position':l.c.slot_index,'content':l.c.title,'price':l.c.price_value,'sales':l.c.sales_count,
             'shop':l.c.shop_name,'captured_at':b.c.captured_at,'page_number':b.c.page_number,'batch':b.c.id}
    # 旧的来源链接按原位置计算展示页码；直接指定批次时保留原位置默认顺序，保证锚点可达。
    statement,spec=sorted_statement(statement,columns,l.c.id,options.get('sort_by'),options.get('order'),
                                    'position' if selected else 'captured_at','asc' if selected else 'desc')
    rows,paging=numbered_rows(connection,statement,options.get('page',1))
    summaries={}
    for row in rows:
        product=row['product_id']
        if product and product not in summaries:
            summaries[product]=detail_summary(connection,tables,product)
        row['detail_context']=observation_context(connection,tables,row,summaries.get(product))
        thumbnail(connection,tables,row)
    return {'observations':rows,**paging,'sort_spec':spec,'selected_batch':allowed.get(selected),
            'summary':summary,'serial_offset':(paging['page']-1)*paging['page_size']}
