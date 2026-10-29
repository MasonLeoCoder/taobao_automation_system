"""长期关注的事务及来源查询；只读原采集表，写入仅限关注表。"""
from contextlib import contextmanager
from uuid import uuid4
import sqlalchemy as sa
from ecommerce.infrastructure.storage.collection_store import collection_store
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error
from ecommerce.infrastructure.storage.time_values import database_now


@contextmanager
def connection(settings, write=False):
    """建立关注事务并屏蔽SQL凭据；参数：配置、写入标记；返回：连接和表。"""
    store = collection_store(settings)
    try:
        with (store.engine.begin() if write else store.engine.connect()) as conn:
            yield conn, store.tables
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def visible_keyword(batches):
    """限定可公开回看的关键词列表；参数：批次表；返回：SQL条件。"""
    return sa.and_(batches.c.visibility == 'published', batches.c.module == 'list',
                   batches.c.search_method == 'keyword', batches.c.search_keyword.is_not(None), batches.c.search_keyword != '')


def observation(conn, tables, identifier):
    """按观察读取整条来源；参数：连接、表、观察编号；返回：字典或空。"""
    l, b, p = (tables[n] for n in ('list_observations', 'collection_batches', 'products'))
    row = conn.execute(sa.select(l, p.c.platform_product_id, b.c.captured_at, b.c.search_keyword,
        b.c.task_id, b.c.page_number).join(b, l.c.batch_id == b.c.id).join(p, p.c.id == l.c.product_id)
        .where(l.c.id == identifier, visible_keyword(b))).mappings().one_or_none()
    return dict(row) if row else None


def states(settings, product_ids):
    """读取样本关注版本；参数：配置、商品编号；返回：商品编号到关注编号的映射。"""
    ids = sorted({i for i in product_ids if i is not None})
    if not ids:
        return {}
    with connection(settings) as (conn, tables):
        f = tables['product_favorites']
        return {str(r.product_id): r.id for r in conn.execute(sa.select(f.c.product_id, f.c.id).where(f.c.product_id.in_(ids)))}


def add(settings, product_id, observation_id):
    """幂等关注并核验观察归属；参数：配置、商品、观察；返回：关注记录。"""
    with connection(settings, True) as (conn, tables):
        # 商品行锁串行化同商品的添加、取消、撤销和备注，防并发覆盖原关注。
        p, f = tables['products'], tables['product_favorites']
        identity = conn.execute(sa.select(p).where(p.c.id == product_id).with_for_update()).mappings().one_or_none()
        source = observation(conn, tables, observation_id)
        if not identity or not identity['platform_product_id'] or not source or source['product_id'] != product_id:
            raise ValueError('商品身份或采集记录不匹配，仅可关注已发布关键词列表中的已确认商品')
        old = conn.execute(sa.select(f).where(f.c.product_id == product_id)).mappings().one_or_none()
        if old:
            return dict(old)
        now = database_now()
        values = dict(id=uuid4().hex, product_id=product_id, observation_id=observation_id,
                      source_keyword=source['search_keyword'], note='', created_at=now, updated_at=now)
        conn.execute(f.insert().values(**values))
        return values


def change(settings, product_id, *, note=None, expected_id=None, remove=False, restore=None):
    """修改、取消或撤销关注；参数：配置、商品及操作；返回：原/新记录，过期页面不得改新关注。"""
    with connection(settings, True) as (conn, tables):
        p, f = tables['products'], tables['product_favorites']
        if not conn.scalar(sa.select(p.c.id).where(p.c.id == product_id).with_for_update()):
            raise LookupError('商品不存在')
        old = conn.execute(sa.select(f).where(f.c.product_id == product_id)).mappings().one_or_none()
        if restore is not None:
            if old:
                raise ValueError('该商品已重新关注，不能覆盖现有记录')
            source = observation(conn, tables, restore['observation_id'])
            if not source or source['product_id'] != product_id:
                raise ValueError('原关注依据已不可用，无法撤销')
            conn.execute(f.insert().values(**restore))
            return restore
        if not old:
            raise LookupError('关注已被取消，请刷新页面')
        if expected_id and old['id'] != expected_id:
            raise ValueError('关注记录已变化，请刷新页面后重试')
        if remove:
            conn.execute(f.delete().where(f.c.id == old['id']))
            return dict(old)
        conn.execute(f.update().where(f.c.id == old['id']).values(note=note, updated_at=database_now()))
        return dict(old, note=note)


def read_page(settings, filters):
    """分页读取关注及加入/最近完整行；参数：配置、筛选；返回：条目、筛选项和分页，历史批量查询。"""
    from ecommerce.shared.pagination import PAGE_SIZE, pagination
    with connection(settings) as (conn, tables):
        f, l, b, p = (tables[n] for n in ('product_favorites', 'list_observations', 'collection_batches', 'products'))
        ranked = sa.select(l.c.id, sa.func.row_number().over(partition_by=l.c.product_id,
            order_by=(b.c.captured_at.desc(), b.c.id.desc(), l.c.id.desc())).label('rn')).join(b, b.c.id == l.c.batch_id).where(
                l.c.product_id.in_(sa.select(f.c.product_id)), visible_keyword(b)).subquery()
        joined = f.join(p, p.c.id == f.c.product_id).outerjoin(l, sa.and_(l.c.product_id == f.c.product_id,
            l.c.id.in_(sa.select(ranked.c.id).where(ranked.c.rn == 1)))).outerjoin(b, b.c.id == l.c.batch_id)
        conditions = []
        if filters['q']:
            value = filters['q']
            conditions.append(sa.or_(l.c.title.contains(value, autoescape=True), p.c.platform_product_id == value))
        if filters['keyword']:
            conditions.append(f.c.source_keyword == filters['keyword'])
        if filters['shop']:
            conditions.append(l.c.shop_name == filters['shop'])
        total = conn.scalar(sa.select(sa.func.count()).select_from(joined).where(*conditions))
        page = min(filters['page'], max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
        info = pagination(total, page)
        order = {'created_desc': f.c.created_at.desc(), 'created_asc': f.c.created_at.asc(),
                 'latest_desc': b.c.captured_at.desc(), 'latest_asc': b.c.captured_at.asc()}[filters['sort']]
        records = [dict(r) for r in conn.execute(sa.select(f, l.c.id.label('latest_id')).select_from(joined).where(*conditions)
            .order_by(order, f.c.id).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).mappings()]
        keywords = list(conn.scalars(sa.select(f.c.source_keyword).distinct().order_by(f.c.source_keyword)))
        shops = [v for v in conn.scalars(sa.select(l.c.shop_name).select_from(joined).distinct().order_by(l.c.shop_name)) if v]
        if not records:
            return {'items': [], 'pagination': info, 'keywords': keywords, 'shops': shops}
        latest_ids = [r['latest_id'] for r in records if r['latest_id'] is not None]
        observation_ids = set(latest_ids) | {r['observation_id'] for r in records}
        rows = [dict(r) for r in conn.execute(sa.select(l, p.c.platform_product_id, b.c.captured_at,
            b.c.search_keyword, b.c.task_id, b.c.page_number).join(b, b.c.id == l.c.batch_id).join(p, p.c.id == l.c.product_id)
            .where(l.c.id.in_(observation_ids), visible_keyword(b))).mappings()]
        by_id = {r['id']: r for r in rows}
        latest = {r['product_id']: r for r in rows if r['id'] in latest_ids}
        return {'items': [{**r, 'initial': by_id.get(r['observation_id']), 'latest': latest.get(r['product_id'])} for r in records],
                'pagination': info, 'keywords': keywords, 'shops': shops}
