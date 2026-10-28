"""搜索词分析的只读数据来源；只使用已发布的关键词列表批次。"""
from collections import defaultdict

import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_store import collection_store
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error


def load_search_sample(settings, *, keyword='', snapshot='', since=None, until=None):
    """读取分析样本；参数：配置、搜索词、快照和北京时间边界；返回：范围、批次与原始观察。"""
    store = collection_store(settings)
    try:
        with store.engine.connect() as connection:
            return read_search_sample(connection, store.tables, keyword=keyword, snapshot=snapshot, since=since, until=until)
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def read_search_sample(connection, tables, *, keyword='', snapshot='', since=None, until=None):
    """在同一连接读取样本；参数：连接、表及范围；返回：未裁分页的真实观察，不写数据库。"""
    batches, listing, products = (tables[name] for name in ('collection_batches', 'list_observations', 'products'))
    statement = sa.select(batches.c.id, batches.c.task_id, batches.c.search_keyword, batches.c.captured_at,
        batches.c.page_number, batches.c.sort_label, batches.c.requested_limit, batches.c.status,
        batches.c.coverage_status, batches.c.source_batch_key).where(
            batches.c.visibility == 'published', batches.c.module == 'list', batches.c.search_method == 'keyword',
            batches.c.search_keyword.is_not(None), batches.c.search_keyword != '')
    if since is not None:
        statement = statement.where(batches.c.captured_at >= since)
    if until is not None:
        statement = statement.where(batches.c.captured_at < until)
    available = [dict(row) for row in connection.execute(statement.order_by(
        batches.c.captured_at.desc(), batches.c.id.desc())).mappings()]
    keywords = list(dict.fromkeys(row['search_keyword'] for row in available))
    chosen_keyword = keyword or (keywords[0] if keywords else '')
    matching = [row for row in available if row['search_keyword'] == chosen_keyword]
    groups = defaultdict(list)
    for row in matching:
        key = 'task:' + row['task_id'] if row['task_id'] else 'batch:' + str(row['id'])
        groups[key].append(row)
    snapshots = [{'key': key, 'captured_at': rows[0]['captured_at'], 'batch_count': len(rows),
                  'pages': sorted({row['page_number'] for row in rows if row['page_number'] is not None}),
                  'task_id': rows[0]['task_id']} for key, rows in groups.items()]
    selected = snapshot or 'range'
    if selected == 'latest':
        selected = snapshots[0]['key'] if snapshots else ''
    if selected == 'range':
        selected_batches = matching
    elif selected:
        if selected not in groups:
            raise LookupError('所选采集快照不属于当前搜索词和日期范围，或尚无已发布列表')
        selected_batches = groups[selected]
    else:
        selected_batches = []
    observations = []
    if selected_batches:
        batch_ids = [row['id'] for row in selected_batches]
        statement = sa.select(listing, products.c.platform_product_id).outerjoin(
            products, products.c.id == listing.c.product_id).where(listing.c.batch_id.in_(batch_ids))
        observations = [dict(row) for row in connection.execute(statement).mappings()]
    return {'keywords': keywords, 'keyword': chosen_keyword, 'snapshots': snapshots, 'snapshot': selected,
            'batches': selected_batches, 'observations': observations}


def attach_previews(settings, rows):
    """补充当前商品页的来源与缩略图；参数：配置和观察行；返回：原位补充，不更新任何记录。"""
    from ecommerce.infrastructure.storage.list_image_preview import thumbnail
    from ecommerce.shared.pagination import PAGE_SIZE
    store = collection_store(settings)
    listing = store.tables['list_observations']
    try:
        with store.engine.connect() as connection:
            for row in rows:
                thumbnail(connection, store.tables, row)
                position = row.get('slot_index')
                preceding = sa.or_(listing.c.slot_index < position,
                    sa.and_(listing.c.slot_index == position, listing.c.id < row['id'])) if position is not None else sa.or_(
                    listing.c.slot_index.is_not(None), sa.and_(listing.c.slot_index.is_(None), listing.c.id < row['id']))
                count = connection.scalar(sa.select(sa.func.count()).select_from(listing).where(
                    listing.c.batch_id == row['batch_id'], preceding))
                row['source_page'] = count // PAGE_SIZE + 1
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None
