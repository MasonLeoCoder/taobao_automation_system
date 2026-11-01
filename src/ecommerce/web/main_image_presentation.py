"""主图画廊的只读展示模型，不改采集资料。"""
from ecommerce.web.basic_batch_presentation import LABELS


def present(data, selected, detail):
    """整理已加载主图批次；参数：商品、批次、校验后记录；返回：画廊显示内容。"""
    state=(selected or {}).get('status');complete=state in {'complete','downloaded'}
    view={'complete':complete,'state_label':LABELS.get(state,'状态未记录'),
          'state_style':'is-ready' if complete else 'is-error' if state in {'failed','failed_skipped'} else 'is-pending',
          'scope_label':'最近有效批次' if selected and complete and selected['id'] in data['current_ids'] else '历史批次' if complete else '采集尝试'}
    view['choices'] = [dict(row, state_label=LABELS.get(row['status'], '状态待核实'))
                       for row in data['history'] if row['module'] == 'main_images']
    view['total'] = (detail or {}).get('total', 0)
    view['note'] = (detail or {}).get('batch_note') or ''
    view['brief_reason'] = view['note'][:160] + ('…' if len(view['note']) > 160 else '')
    view['items'] = []
    for row in (detail or {}).get('records', []):
        if row.get('role') != 'main_image':
            continue
        asset = row.get('file') or {}
        usable = row.get('preview_status') == 'present' and bool(asset.get('id'))
        view['items'].append({**row, 'number': row.get('position') or row['id'], 'usable': usable,
                              'asset_id': asset.get('id') if usable else None,
                              'local_label': '原件可用' if usable else {'missing':'本地原件缺失','changed':'原件校验未通过'}.get(row.get('preview_status'), '暂无可用原件')})
    return view
