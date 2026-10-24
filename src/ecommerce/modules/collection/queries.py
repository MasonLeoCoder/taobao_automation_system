"""采集查询的公开业务入口，统一 CLI/Web 的数据库启用状态和范围。"""
from ecommerce.infrastructure.storage import collection_queries as storage


def query(settings,query_type,**parameters):
    """查询商品、批次或明细；参数：配置、种类和已解析筛选；返回：统一查询结果。"""
    if settings.collection_backend!='mysql':
        raise ValueError('当前采集数据库尚未启用，请完成历史核对和配置切换')
    actions={'products':storage.products,'batches':storage.batches,'records':storage.records}
    if query_type not in actions:
        raise ValueError('不支持的采集查询')
    return actions[query_type](settings,**parameters)
