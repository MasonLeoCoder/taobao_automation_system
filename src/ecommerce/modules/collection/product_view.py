"""商品资料页的只读展示摘要，不更改采集判定或批次来源。"""

from ecommerce.modules.collection.web_collection import MODULES, COMPLETE_STATES


def module_summaries(data, display_history, context_run=None):
    """整理导航数量及资料状态；参数：商品资料、范围内历史、关联运行；返回：按模块的摘要。"""
    result = {}
    current = set(data['current_ids'])
    for module in MODULES:
        rows = [row for row in display_history if row['module'] == module]
        latest = next(iter(rows), None)
        valid = next((row for row in rows if row['status'] in COMPLETE_STATES
                      and (context_run or row['id'] in current)), None)
        counts = (valid or {}).get('record_counts', {})
        count = None
        if valid and module != 'basic_info':
            count = counts.get('tags' if module == 'review_summary' else 'records', 0)
        status = ('已确认无内容' if valid and valid['status'].startswith('no_') else
                  '已有资料' if valid else '采集异常' if latest and latest['status'] in
                  {'failed', 'failed_skipped', 'partial', 'not_run_skipped', 'discarded'} else
                  '已确认无入口' if not context_run and module in data.get('no_entry_modules', []) else
                  '暂无有效资料' if latest else '尚未采集')
        result[module] = {'latest': latest, 'valid': valid, 'count': count, 'status': status}
    return result
