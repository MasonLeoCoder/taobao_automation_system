"""商品命令适配及终端交互。"""
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.shared.config import Settings


def execute(settings: Settings, start: bool, wait: bool, keyword: str | None = None, category: str | None = None, brand: str | None = None):
    """提交发布入口任务；参数：配置、开启开关、等待开关、搜索词、类目、品牌；返回：任务结果。"""
    from ecommerce.modules.product.service import open_publish
    on_ready = (lambda: input("页面已就绪，按回车关闭浏览器：")) if wait else None
    return run_task(settings, "product.open", lambda: open_publish(settings, start, on_ready, keyword, category, brand))
