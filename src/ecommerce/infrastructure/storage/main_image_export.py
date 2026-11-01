"""读取指定商品已发布主图或详情图批次索引；保留原主图入口兼容，不修改记录。"""
import sqlalchemy as sa
from ecommerce.infrastructure.storage.collection_store import collection_store
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error


def sources(settings, product_id, batch_id, *, media_id=None, module='main_images'):
    """读取固定图片模块及原编号；参数：配置、商品、批次、媒体编号和模块；返回：文件索引。"""
    roles={'main_images':'main_image','detail_images':'detail_image'}
    if module not in roles:raise ValueError('不支持的图片导出模块')
    store = collection_store(settings)
    b, p, m, f = (store.tables[name] for name in ('collection_batches','products','media_references','file_assets'))
    try:
        with store.engine.connect() as connection:
            exists = connection.scalar(sa.select(b.c.id).join(p, p.c.id == b.c.product_id).where(
                b.c.id == batch_id, b.c.module == module, b.c.visibility == 'published',
                p.c.platform == 'taobao', p.c.platform_product_id == product_id))
            if exists is None:
                raise LookupError('图片批次不存在或不属于此商品模块')
            statement = sa.select(m.c.id.label('media_id'), m.c.position, *f.c).select_from(
                m.outerjoin(f, f.c.id == m.c.file_asset_id)).where(m.c.batch_id == batch_id, m.c.role == roles[module])
            if media_id is not None:
                statement=statement.where(m.c.id==media_id)
            rows = connection.execute(statement.order_by(m.c.position, m.c.id).limit(201)).mappings()
            return [dict(row) for row in rows]
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None
