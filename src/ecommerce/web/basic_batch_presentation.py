"""基本信息单批次的中文状态和来源展示，不改当前有效指针。"""
LABELS={'complete':'已完成','downloaded':'已完成','partial':'部分完成','failed':'采集失败',
        'failed_skipped':'失败，已跳过','not_run_skipped':'未执行','not_run':'未执行',
        'running':'执行中','pending':'等待执行','cancelled':'已停止','interrupted':'已中断',
        'needs_attention':'待人工处理','discarded':'未完成明细已清理','resolved':'已补采解决'}


def present(data,selected,detail):
    """整理已校验批次的只读展示；参数：商品、批次、明细；返回：选项和本批记录。"""
    choices=[dict(row,state_label=LABELS.get(row['status'],'状态待核实')) for row in data['history'] if row['module']=='basic_info']
    record=next(iter((detail or {}).get('records',[])),None)
    state=(selected or {}).get('status')
    complete=state in {'complete','downloaded'}
    current=bool(selected and complete and selected['id'] in data['current_ids'])
    note=(detail or {}).get('batch_note') or ''
    return {'choices':choices,'record':record,'complete':complete,'current':current,
            'state_label':LABELS.get(state,'状态未记录'),'state_style':'is-ready' if complete else 'is-error' if state in {'failed','failed_skipped'} else 'is-active' if state in {'running','pending'} else 'is-pending',
            'scope_label':'最近有效批次' if current else '历史批次' if complete else '采集尝试',
            'brief_reason':note[:160]+('…' if len(note)>160 else ''),'note':note}
