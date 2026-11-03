"""交叉分析复用持久调用、预算保护与本地检查执行器。"""
from ecommerce.modules.composite_analysis import runtime as shared
from . import planner, report


def execute(settings, task_id, events, client_factory):
    """仅执行已确认固定计划，零自动重试或扩额。"""
    return shared.execute(settings,task_id,events,client_factory,planning=planner,reporting=report)


def recheck(settings, data, report_id):
    """明确免费重查，追加版本，保留旧报告及原响应。"""
    return shared.recheck(settings,data,report_id,reporting=report)
