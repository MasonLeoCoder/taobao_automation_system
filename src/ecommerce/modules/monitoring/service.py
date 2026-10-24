"""命令行与网页共享的只读运行状态查询。"""
from ecommerce.infrastructure.storage.database import list_tasks, task_counts
from ecommerce.shared.config import Settings
from ecommerce.shared.features import FEATURES


def recent_tasks(settings: Settings) -> list[dict]:
    """查询近期任务；参数：配置；返回：任务记录列表。"""
    return list_tasks(settings.task_database)


def overview(settings: Settings) -> dict:
    """查询工作台状态；参数：配置；返回：功能、任务统计和近期记录。"""
    counts = task_counts(settings.task_database)
    return {"features": FEATURES, "tasks": recent_tasks(settings), "counts": counts,
            "total": sum(counts.values()), "shop_name": settings.shop_name,
            "has_session_file": settings.session_path.is_file()}
