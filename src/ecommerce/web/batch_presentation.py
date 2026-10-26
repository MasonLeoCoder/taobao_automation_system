"""模块采集状态与本批实际数量的展示规则。"""


def batch_scope_text(batch):
    """组织状态后的说明；参数：含只读数量的批次；返回：中文文本，不推断完成状态。"""
    module=batch.get('module')
    counts=batch.get('record_counts') or {}
    parts=[]
    units={'basic_info':'条基本信息','list':'条列表记录','video':'个视频','main_images':'张图片',
        'detail_images':'张图片','skus':'种规格','parameters':'项参数','reviews':'条评价','questions':'个问题'}
    count=counts.get('records')
    empty=batch.get('status','').startswith('no_') or batch.get('status')=='empty'
    if not empty and module in units and count is not None:
        text=f'{count}{units[module]}'
        if module=='skus' and counts.get('out_of_stock'):
            text+=f"，其中{counts['out_of_stock']}种缺货"
        if module=='questions' and counts.get('answers') is not None:
            text+=f"、{counts['answers']}条回答"
        parts.append(text)
    if not empty and module=='review_summary' and counts.get('tags') is not None:
        parts.append(f"{counts['tags']}个评价标签")
    coverage=batch.get('coverage_status')
    descriptions={'limit_reached':'已达到本次上限','visible_end_confirmed':'已到达可见末尾',
        'manual_count_confirmed':'已人工确认采集范围','confirmed_empty':'已确认无数据','incomplete':'采集范围未完成'}
    if coverage in descriptions and coverage!=batch.get('status') and not (empty and coverage=='confirmed_empty'):
        # 采集未完成已由状态说明，避免同一行重复提示。
        if not (coverage=='incomplete' and batch.get('status') in {'partial','failed','needs_attention','cancelled'}):
            parts.append(descriptions[coverage])
    elif not empty and coverage in {None,'unknown',''} and module in {'reviews','questions','list'}:
        parts.append('采集范围未记录')
    if not empty and module in {'reviews','questions','list'}:
        if batch.get('collection_mode')=='limited' and batch.get('requested_limit') is not None:
            parts.append(f"本次上限 {batch['requested_limit']}")
        elif batch.get('collection_mode')=='all':
            parts.append('本次未设数量上限')
    return ' · '.join(parts)
