"""商品参数页的只读批次标识和数量，不修改参数原文。"""
from ecommerce.web.basic_batch_presentation import LABELS


def present(data,selected,detail):
    """整理本批状态和历史选项；参数：商品、批次、明细；返回：页面模型。"""
    labels={**LABELS,'complete':'采集完成','no_parameters':'已确认无参数'}
    choices=[dict(row,state_label=labels.get(row['status'],'状态待核实')) for row in data['history'] if row['module']=='parameters']
    state=(selected or {}).get('status');complete=state in {'complete','no_parameters'}
    error=bool(selected and detail is None)
    current=bool(selected and complete and selected['id'] in data['current_ids'])
    return {'choices':choices,'complete':complete,'read_error':error,'total':detail['total'] if detail is not None else None,
        'state_label':'资料读取失败' if error else labels.get(state,'尚未采集'),
        'state_style':'is-error' if error or state in {'failed','failed_skipped'} else 'is-ready' if complete else 'is-pending',
        'scope_label':'最近有效资料' if current else '历史批次' if complete else '采集尝试'}
