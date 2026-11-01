"""读取指定商品已发布主图批次的文件索引；不读取大原始JSON或修改任何记录。"""
import sqlalchemy as sa
from ecommerce.infrastructure.storage.collection_store import collection_store
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error


def sources(settings, product_id, batch_id, *, media_id=None):
    """读取主图及原编号；参数：配置、平台商品编号、批次和可选媒体编号；返回：文件索引列表。"""
    store = collection_store(settings)
    b, p, m, f = (store.tables[name] for name in ('collection_batches','products','media_references','file_assets'))
    try:
        with store.engine.connect() as connection:
            exists = connection.scalar(sa.select(b.c.id).join(p, p.c.id == b.c.product_id).where(
                b.c.id == batch_id, b.c.module == 'main_images', b.c.visibility == 'published',
                p.c.platform == 'taobao', p.c.platform_product_id == product_id))
            if exists is None:
                raise LookupError('主图批次不存在或不属于此商品')
            statement = sa.select(m.c.id.label('media_id'), m.c.position, *f.c).select_from(
                m.outerjoin(f, f.c.id == m.c.file_asset_id)).where(m.c.batch_id == batch_id, m.c.role == 'main_image')
            if media_id is not None:
                statement=statement.where(m.c.id==media_id)
            rows = connection.execute(statement.order_by(m.c.position, m.c.id).limit(201)).mappings()
            return [dict(row) for row in rows]
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None
