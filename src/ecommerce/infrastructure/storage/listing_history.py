"""查询命令行上架任务的结构化事件及执行时的千牛账号快照。"""
import sqlalchemy as sa

from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error


def cli_listing_events(settings, *, shop_id=None, task_id=None):
    store = settings.task_database
    tasks, events = store.tables['tasks'], store.tables['task_events']
    started = events.alias('listing_task_started')
    statement = (sa.select(events.c.task_id, events.c.event_type, events.c.occurred_at, events.c.details,
                           tasks.c.started_at, started.c.details.label('source_details'))
                 .select_from(events.join(tasks, tasks.c.id == events.c.task_id).join(started, sa.and_(
                     started.c.task_id == tasks.c.id, started.c.task_sequence == 1,
                     started.c.event_type == 'task_started')))
                 .where(tasks.c.name.in_(('product.open', 'product.submit')),
                        events.c.event_type.in_(('listing_filled', 'listing_submit_intent', 'listing_warehouse')),
                        events.c.details['entry'].as_string() == 'cli')
                 .order_by(events.c.task_id, events.c.task_sequence))
    if shop_id is not None:
        statement = statement.where(tasks.c.shop_id == shop_id)
    if task_id is not None:
        statement = statement.where(tasks.c.id == task_id)
    try:
        with store.engine.connect() as connection:
            return connection.execute(statement).mappings().all()
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None
