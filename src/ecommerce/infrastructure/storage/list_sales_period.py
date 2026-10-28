"""显式维护淘宝搜索列表收货人数周期；常规查询和启动不自动回填。"""
import sqlalchemy as sa

from ecommerce.shared.sales import FIELDS, taobao_received_period_update


def received_period_candidates(connection, tables, *, lock=False):
    """核对可更正来源；参数：连接、表和锁标记；返回：按编号排序的候选清单，不修改数据。"""
    listing, batches = tables['list_observations'], tables['collection_batches']
    statement = sa.select(listing.c.id, listing.c.batch_id, listing.c.product_id, listing.c.slot_index,
        listing.c.sales_text, *[listing.c[name] for name in FIELDS], batches.c.task_id,
        batches.c.search_keyword, batches.c.source_batch_key).join(batches, listing.c.batch_id==batches.c.id).where(
            batches.c.platform=='taobao', batches.c.module=='list', batches.c.search_method=='keyword',
            batches.c.search_keyword.is_not(None), batches.c.search_keyword!='', batches.c.visibility=='published',
            listing.c.sales_metric=='received_people', listing.c.sales_period=='unknown',
            listing.c.sales_parse_status=='parsed').order_by(listing.c.id)
    if lock:
        statement = statement.with_for_update()
    return [dict(row) for row in connection.execute(statement).mappings() if taobao_received_period_update(row)]


def correct_received_periods(connection, tables, expected):
    """更正明确审核过的清单；参数：事务连接、表和候选原值；返回：改动编号，清单变化则拒绝。"""
    current = received_period_candidates(connection,tables,lock=True)
    if current != expected:
        raise ValueError('待更正的列表来源或字段已变化，请重新核对清单')
    table = tables['list_observations']
    changed = []
    for row in current:
        result = connection.execute(table.update().where(table.c.id==row['id'], table.c.sales_period=='unknown')
            .values(sales_period='last_30_days'))
        if result.rowcount != 1:
            raise ValueError('周期更正行数与审核清单不符')
        changed.append(row['id'])
    return changed
