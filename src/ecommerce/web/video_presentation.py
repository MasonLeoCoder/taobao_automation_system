"""视频页的批次和本地原件展示，沿用既有文件校验结果。"""
from pathlib import Path
from ecommerce.web.basic_batch_presentation import LABELS


def present(data,selected,detail):
    """整理所选视频批次；参数：商品、批次及校验后明细；返回：只读界面模型。"""
    labels={**LABELS,'downloaded':'下载完成','no_video':'已确认无视频'}
    choices=[dict(row,state_label=labels.get(row['status'],'状态待核实')) for row in data['history'] if row['module']=='video']
    state=(selected or {}).get('status');complete=state in {'complete','downloaded','no_video'}
    items=[]
    for row in (detail or {}).get('records',[]):
        if row.get('role')!='main_video':continue
        local=row.get('preview_status','unavailable');asset=row.get('file') or {}
        usable=local=='present' and bool(asset.get('id')) and Path(asset.get('relative_path','')).suffix.lower() in {'.mp4','.webm'}
        items.append({**row,'usable':usable,'asset_id':asset.get('id') if usable else None,
                      'local_label':'本地文件可用' if usable else {'missing':'本地文件缺失','changed':'文件校验未通过'}.get(local,'暂无可用本地文件')})
    note=(detail or {}).get('batch_note') or ''
    return {'choices':choices,'items':items,'total':(detail or {}).get('total',0),'complete':complete,
            'state_label':labels.get(state,'状态未记录'),'state_style':'is-ready' if complete else 'is-error' if state in {'failed','failed_skipped'} else 'is-pending',
            'scope_label':'最近有效批次' if selected and complete and selected['id'] in data['current_ids'] else '历史批次' if complete else '采集尝试',
            'note':note,'brief_reason':note[:160]+('…' if len(note)>160 else '')}
