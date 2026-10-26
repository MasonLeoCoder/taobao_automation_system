"""命令行与网页共享的只读运行状态查询。"""
from ecommerce.infrastructure.storage.database import list_tasks, task_counts
from ecommerce.shared.config import Settings
from ecommerce.shared.features import FEATURES


def recent_tasks(settings: Settings) -> list[dict]:
    """查询近期任务；参数：配置；返回：任务记录列表。"""
    return list_tasks(settings.task_database)


def task_history(settings: Settings, page: int = 1, *, sort_by=None,order=None) -> dict:
    """分页查询全部任务；参数：配置、从1开始页码；返回：每页20条及分页信息。"""
    if type(page) is not int or page<1:
        raise ValueError('页码须为正整数')
    source=settings.task_database
    total=sum(task_counts(source).values())
    from ecommerce.shared.pagination import PAGE_SIZE,pagination
    info=pagination(total,page)
    from ecommerce.shared.sorting import sorting
    spec=sorting(sort_by,order,{'name','status','started_at','finished_at','message'},'started_at','desc')
    return {'tasks':list_tasks(source,PAGE_SIZE,offset=(page-1)*PAGE_SIZE,sort_by=sort_by,order=order),**info,'sort_spec':spec,
            'features':FEATURES,'shop_name':settings.shop_name}


def overview(settings: Settings) -> dict:
    """查询工作台状态；参数：配置；返回：功能、任务统计和近期记录。"""
    counts = task_counts(settings.task_database)
    return {"features": FEATURES, "tasks": recent_tasks(settings), "counts": counts,
            "total": sum(counts.values()), "shop_name": settings.shop_name,
            "has_session_file": settings.session_path.is_file()}
