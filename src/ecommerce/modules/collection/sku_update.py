"""旧SKU概况的兼容入口，所有更新状态由公共模型计算。"""
from ecommerce.modules.collection.module_update import stamp

def build(data,states=()):
    """构建SKU更新；参数：商品与任务；返回：公共模型。"""
    from ecommerce.modules.collection.module_update import build as module_build
    return module_build(data,states,'skus')
