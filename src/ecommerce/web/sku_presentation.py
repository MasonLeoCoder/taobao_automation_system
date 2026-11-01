"""SKU页面批次标签与界面上下文，规格解释复用业务层。"""
from ecommerce.modules.collection.sku_view import present_record, present_summary
from ecommerce.web.basic_batch_presentation import LABELS


def present(data, selected, detail, stock_filter='all'):
    """整理SKU页面；参数：商品、已加载批次、明细及状态筛选；返回：临时显示模型。"""
    state=(selected or {}).get('status');complete=state=='complete'
    note=(detail or {}).get('batch_note') or ''
    return {'choices':[dict(row,state_label=LABELS.get(row['status'],'状态待核实')) for row in data['history'] if row['module']=='skus'],
            'complete':complete,'state_label':LABELS.get(state,'状态未记录'),
            'state_style':'is-ready' if complete else 'is-error' if state in {'failed','failed_skipped'} else 'is-pending',
            'scope_label':'最近有效批次' if selected and complete and selected['id'] in data['current_ids'] else '历史批次' if complete else '采集尝试',
            'note':note,'brief_reason':note[:160]+('…' if len(note)>160 else ''),'stock_filter':stock_filter,
            'items':[present_record(row) for row in (detail or {}).get('records',[])],
            'overview':present_summary(detail['sku_overview']) if detail else None}
