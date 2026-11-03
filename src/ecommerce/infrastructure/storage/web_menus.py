"""Web菜单配置只读存储；不在请求或启动时初始化、补写菜单。"""
import sqlalchemy as sa
from ecommerce.infrastructure.storage.task_store import task_store
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error


def read(settings):
    """在同一读取快照查询两表；参数：MySQL连接配置；返回：分组与项目原值。"""
    store = task_store(settings)
    try:
        with store.engine.connect() as connection:
            if connection.dialect.name == 'mysql':
                connection = connection.execution_options(isolation_level='REPEATABLE READ')
            groups = [dict(row) for row in connection.execute(sa.select(store.tables['web_menu_groups'])).mappings()]
            items = [dict(row) for row in connection.execute(sa.select(store.tables['web_menu_items'])).mappings()]
        return groups, items
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError('菜单配置读取失败：'+safe_error(error)) from None


def resource_owners(settings, *, batch_id=None, file_id=None):
    """按已发布来源判定共享资源归属；参数：连接、批次或文件；返回：可服务的功能标识集合。"""
    store = task_store(settings)
    batches = store.tables['collection_batches']
    statement = sa.select(batches.c.module).where(batches.c.visibility == 'published')
    if batch_id is not None:
        statement = statement.where(batches.c.id == batch_id)
    else:
        media = store.tables['media_references']
        statement = statement.join(media, media.c.batch_id == batches.c.id).where(media.c.file_asset_id == file_id)
    try:
        with store.engine.connect() as connection:
            modules = set(connection.scalars(statement))
            owners = {'searches' if value == 'list' else 'catalog' for value in modules}
            if file_id is not None and 'list' in modules:
                # 列表缩略图同样用于分析、关注和商品库；只停用列表页不应破坏这些已启用页面的图片。
                owners.update({'analysis', 'favorites', 'catalog'})
            return owners
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None
