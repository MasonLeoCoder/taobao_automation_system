"""搜索排行快照的事务写入与按批读取。"""
from contextlib import contextmanager

import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_store import collection_store
from ecommerce.infrastructure.storage.environments import assert_writable
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error
from ecommerce.infrastructure.storage.time_values import database_now

WORD_TYPES = {'search': '搜索词', 'trend': '趋势词', 'core': '核心词', 'modifier': '修饰词'}
PERIODS = {'7d': '7天', '30d': '30天', 'day': '日'}
PAGE_SIZE = 50


@contextmanager
def connection(settings, *, write=False):
    if write:
        assert_writable(settings.mysql)
    store = collection_store(settings)
    try:
        with (store.engine.begin() if write else store.engine.connect()) as conn:
            yield conn, store.tables
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def categories(settings):
    with connection(settings) as (conn, tables):
        table = tables['publish_categories']
        return [dict(row) for row in conn.execute(sa.select(table).order_by(table.c.full_path)).mappings()]


def begin(settings, identifier, task_id, category, period, pages, word_types):
    with connection(settings, write=True) as (conn, tables):
        batches, types = tables['sycm_search_batches'], tables['sycm_search_batch_types']
        conn.execute(batches.insert().values(id=identifier, task_id=task_id,
            publish_category_id=category['id'], publish_category_path=category['full_path'],
            ranking_type='popularity', period_type=period, requested_pages=pages,
            status='running', started_at=database_now()))
        return {word_type: conn.execute(types.insert().values(batch_id=identifier, word_type=word_type,
            status='pending', collected_pages=0, row_count=0)).inserted_primary_key[0] for word_type in word_types}


def confirm_scope(settings, identifier, path, platform_id, start, end):
    with connection(settings, write=True) as (conn, tables):
        batch = tables['sycm_search_batches']
        conn.execute(batch.update().where(batch.c.id == identifier, batch.c.status == 'running').values(
            sycm_category_path=path, sycm_category_id=platform_id, period_start=start, period_end=end))


def save_page(settings, type_id, page, entries, captured_at):
    """明细和覆盖数同事务提交，按页顺序写入。"""
    with connection(settings, write=True) as (conn, tables):
        types, detail = tables['sycm_search_batch_types'], tables['sycm_search_rank_entries']
        row = conn.execute(sa.select(types).where(types.c.id == type_id).with_for_update()).mappings().one()
        if row['status'] != 'pending' or row['collected_pages'] + 1 != page or not entries:
            raise ValueError('排行页写入次序或状态不正确')
        conn.execute(detail.insert(), [
            dict(item, batch_type_id=type_id, page_no=page, row_no=index, captured_at=captured_at)
            for index, item in enumerate(entries, 1)])
        conn.execute(types.update().where(types.c.id == type_id).values(
            collected_pages=page, row_count=row['row_count'] + len(entries)))


def finish_type(settings, type_id, reason, failed_page=None, error=None):
    with connection(settings, write=True) as (conn, tables):
        types = tables['sycm_search_batch_types']
        row = conn.execute(sa.select(types).where(types.c.id == type_id).with_for_update()).mappings().one()
        status = ('complete' if reason in {'end', 'empty', 'requested'} else 'changed' if reason == 'changed'
                  else 'partial' if row['row_count'] else 'failed')
        conn.execute(types.update().where(types.c.id == type_id).values(
            status=status, end_reason=reason, failed_page=failed_page, error_message=error[:500] if error else None))


def finish(settings, identifier, error=None):
    with connection(settings, write=True) as (conn, tables):
        batches, types = tables['sycm_search_batches'], tables['sycm_search_batch_types']
        rows = conn.execute(sa.select(types).where(types.c.batch_id == identifier).with_for_update()).mappings().all()
        for row in rows:
            if row['status'] == 'pending':
                conn.execute(types.update().where(types.c.id == row['id']).values(
                    status='partial' if row['row_count'] else 'failed', end_reason='interrupted',
                    error_message=error[:500] if error else '采集未完成'))
        status = ('complete' if all(row['status'] == 'complete' for row in rows) else
                  'partial' if any(row['row_count'] for row in rows) else 'failed')
        conn.execute(batches.update().where(batches.c.id == identifier).values(status=status, finished_at=database_now()))
        return status


def browse(settings, category_id, period, batch_id=None, word_type='search', page=1, *, export=False):
    with connection(settings) as (conn, tables):
        batches, types, detail = (tables[name] for name in
            ('sycm_search_batches', 'sycm_search_batch_types', 'sycm_search_rank_entries'))
        has_rows = sa.exists(sa.select(types.c.id).where(types.c.batch_id == batches.c.id, types.c.row_count > 0))
        captured = sa.select(sa.func.coalesce(sa.func.sum(types.c.collected_pages), 0)).where(
            types.c.batch_id == batches.c.id).scalar_subquery()
        history = [dict(row) for row in conn.execute(sa.select(batches, has_rows.label('has_rows'),
            captured.label('captured_pages')).where(
            batches.c.publish_category_id == category_id, batches.c.period_type == period
        ).order_by(batches.c.started_at.desc(), batches.c.id.desc())).mappings()]
        selected = next((row for row in history if row['id'] == batch_id), None) if batch_id else next(
            (row for row in history if row['has_rows'] and row['status'] != 'running'), None)
        if batch_id and not selected:
            raise LookupError('找不到此类目及周期下的采集批次')
        result = {'history': history, 'batch': selected, 'types': {}, 'entries': [],
                  'page': 1, 'pages': 1, 'page_size': PAGE_SIZE}
        if not selected:
            return result
        summaries = conn.execute(sa.select(types).where(types.c.batch_id == selected['id'])).mappings()
        result['types'] = {row['word_type']: dict(row) for row in summaries}
        selected['captured_rows'] = sum(row['row_count'] for row in result['types'].values())
        current = result['types'].get(word_type)
        if not current:
            return result
        query = sa.select(detail).where(detail.c.batch_type_id == current['id']).order_by(detail.c.page_no, detail.c.row_no)
        if not export:
            pages = max(1, (current['row_count'] + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(1, min(pages, page))
            result.update(page=page, pages=pages)
            query = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        result['entries'] = [dict(row) for row in conn.execute(query).mappings()]
        return result
