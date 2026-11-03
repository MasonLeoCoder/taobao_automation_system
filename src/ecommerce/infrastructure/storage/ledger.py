"""账本数据库事务：只操作三张账本表，不访问平台或AI。"""
from contextlib import contextmanager
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert
from ecommerce.infrastructure.storage.collection_store import collection_store
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error
from ecommerce.infrastructure.storage.time_values import database_now


@contextmanager
def connection(settings, write=False):
    """打开账本连接；参数：配置、是否写入；返回：事务连接及模型。"""
    store = collection_store(settings)
    try:
        with (store.engine.begin() if write else store.engine.connect()) as conn:
            yield conn, store.tables
    except sa.exc.IntegrityError:
        raise ValueError('分类名称重复或关联记录已变化，请刷新后重试') from None
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def save_entry(settings, identifier, revision, values):
    """幂等新增或并发安全修改；参数：配置、编号、旧版本、规范值；返回：无。"""
    with connection(settings, True) as (conn, tables):
        table, categories = tables['ledger_entries'], tables['ledger_categories']
        category = conn.execute(sa.select(categories).where(categories.c.id == values['category_id']).with_for_update()).mappings().one_or_none()
        old = conn.execute(sa.select(table).where(table.c.id == identifier).with_for_update()).mappings().one_or_none()
        if revision == 0 and old:
            if all(old[key] == value for key, value in values.items()) and old['status'] == 'active':
                return
            raise ValueError('该表单已保存，请刷新后记下一笔')
        if revision and (not old or old['revision'] != revision or old['status'] != 'active'):
            raise ValueError('账目已修改或作废，请刷新后重试')
        if not category or (not category['is_enabled'] and (not old or old['category_id'] != category['id'])):
            raise ValueError('请选择已启用分类')
        now = database_now()
        if revision:
            conn.execute(table.update().where(table.c.id == identifier).values(**values, revision=revision+1, updated_at=now))
        else:
            conn.execute(table.insert().values(id=identifier, **values, status='active', revision=1, created_at=now, updated_at=now))


def void_entry(settings, identifier, revision):
    """作废且保留原记录；参数：配置、编号、旧版本；返回：无。"""
    with connection(settings, True) as (conn, tables):
        table = tables['ledger_entries']
        result = conn.execute(table.update().where(table.c.id == identifier, table.c.revision == revision,
            table.c.status == 'active').values(status='void', revision=revision+1, updated_at=database_now()))
        if result.rowcount != 1:
            raise ValueError('账目已修改或作废，请刷新后重试')


def save_category(settings, identifier, values):
    """新增或修改分类；参数：配置、编号0表示新增、规范值；返回：无。"""
    with connection(settings, True) as (conn, tables):
        table, now = tables['ledger_categories'], database_now()
        if identifier:
            old = conn.execute(sa.select(table.c.id).where(table.c.id == identifier).with_for_update()).first()
            if not old:
                raise ValueError('分类不存在')
            conn.execute(table.update().where(table.c.id == identifier).values(**values, updated_at=now))
        else:
            conn.execute(table.insert().values(**values, created_at=now, updated_at=now))


def save_limit(settings, selected_month, amount):
    """原子保存当月额度；参数：配置、月初日期、金额；返回：无。"""
    with connection(settings, True) as (conn, tables):
        table, now = tables['ledger_monthly_limits'], database_now()
        statement = insert(table).values(month=selected_month, amount=amount, created_at=now, updated_at=now)
        conn.execute(statement.on_duplicate_key_update(amount=amount, updated_at=now))


def overview(settings, start, end, category, direction, status, page):
    """读取一致月度快照；参数：规范化筛选；返回：精确金额、分类汇总与50条分页。"""
    from decimal import Decimal
    with connection(settings) as (conn, tables):
        e, c, limits = (tables[name] for name in ('ledger_entries', 'ledger_categories', 'ledger_monthly_limits'))
        scope = (e.c.occurred_at >= start, e.c.occurred_at < end)
        categories = [dict(row) for row in conn.execute(sa.select(c).order_by(c.c.sort_order, c.c.id)).mappings()]
        totals = {'expense': Decimal('0.00'), 'income': Decimal('0.00')}
        for row in conn.execute(sa.select(e.c.direction, sa.func.sum(e.c.amount)).where(*scope, e.c.status == 'active').group_by(e.c.direction)):
            totals[row[0]] = row[1]
        budget = conn.scalar(sa.select(limits.c.amount).where(limits.c.month == start))
        groups = [dict(row) for row in conn.execute(sa.select(c.c.name, e.c.direction,
            sa.func.sum(e.c.amount).label('amount')).join(c, e.c.category_id == c.c.id)
            .where(*scope, e.c.status == 'active').group_by(c.c.id, c.c.name, e.c.direction).order_by(c.c.name, e.c.direction)).mappings()]
        filters = list(scope)
        if category:
            filters.append(e.c.category_id == category)
        if direction:
            filters.append(e.c.direction == direction)
        if status:
            filters.append(e.c.status == status)
        count = conn.scalar(sa.select(sa.func.count()).select_from(e).where(*filters))
        rows = [dict(row) for row in conn.execute(sa.select(e, c.c.name.label('category_name'))
            .join(c, e.c.category_id == c.c.id).where(*filters).order_by(e.c.occurred_at.desc(), e.c.id.desc())
            .offset((page-1)*50).limit(50)).mappings()]
        return dict(categories=categories, rows=rows, totals=totals, budget=budget,
            balance=totals['income']-totals['expense'], remaining=budget-totals['expense'] if budget is not None else None,
            groups=groups, count=count, page=page, pages=max(1, (count+49)//50))


def get_entry(settings, identifier):
    """读取待编辑明细；参数：配置、编号；返回：记录或空。"""
    with connection(settings) as (conn, tables):
        table = tables['ledger_entries']
        row = conn.execute(sa.select(table).where(table.c.id == identifier)).mappings().one_or_none()
        return dict(row) if row else None
