"""SKU更新的只读概况，区分有效资料、当前申请和本次完整性。"""
from ecommerce.infrastructure.storage.time_values import iso_value

LABELS={'queued':'等待执行','waiting':'等待执行','paused':'队列已暂停','starting':'启动中','running':'采集中',
        'stopping':'正在停止','needs_attention':'待人工处理','storage_pending':'等待入库','unverified':'状态待核实',
        'failed':'更新失败','cancelled':'已停止','interrupted':'已中断','verification_pending':'结果待核对'}


def stamp(value):
    """提供可排序时间；参数：数据库时间或公开时间；返回：ISO字符串或空。"""
    return iso_value(value) if value and not isinstance(value,str) else value or ''


def build(data,states=()):
    """整理当前商品全局SKU更新；参数：商品资料及已核对申请；返回：纯只读概要。"""
    product=data['product']['platform_product_id']
    rows=[row for row in data['history'] if row['module']=='skus']
    current=set(data['current_ids'])
    valid=next((row for row in rows if row['id'] in current and row['status']=='complete' and row.get('record_counts',{}).get('records',0)>0),None)

    def batch(row):
        """只保留需要展示的批次字段；参数：历史行；返回：摘要或空。"""
        return {'id':row['id'],'status':row['status'],'captured_at':stamp(row.get('captured_at')),
                'count':row.get('record_counts',{}).get('records',0),'task_id':row.get('task_id')} if row else None

    related=[job for job in states if job.get('product_id')==product and (job.get('module')=='skus' or 'skus' in (job.get('modules') or []))]
    related.sort(key=lambda job:(stamp(job.get('created_at')),job['id']),reverse=True)
    active=next((job for job in related if not job['done']),None)
    newest=next(iter(related),None);latest=next(iter(rows),None)
    outcome=None
    if active:
        outcome={'state':active['state'],'label':LABELS.get(active['state'],'状态待核实'),
                 'message':active.get('message',''),'task_id':active.get('task_id'),'job_id':active['id'],'batch':None,'complete':False}
    elif newest and (not latest or (newest.get('task_id') or newest['id'])!=latest.get('task_id') and stamp(newest.get('created_at'))>stamp(latest.get('captured_at'))):
        outcome={'state':newest['state'],'label':LABELS.get(newest['state'],'结果待核对'),
                 'message':newest.get('message',''),'task_id':newest.get('task_id'),'job_id':newest['id'],'batch':None,'complete':False}
    elif latest:
        complete=bool(valid and valid['id']==latest['id'])
        if complete:label='更新完成'
        elif latest['status']=='partial':label='部分完成'
        elif latest['status'] in {'failed','cancelled','interrupted'}:label=LABELS[latest['status']]
        else:label='结果待核对'
        message=('此前有效资料仍保留。' if valid else '本次尚未形成完整有效SKU资料。') if not complete else ''
        outcome={'state':'complete' if complete else latest['status'],'label':label,'message':message,
                 'task_id':latest.get('task_id'),'job_id':None,'batch':batch(latest),'complete':complete}
    return {'latest_valid':batch(valid),'latest_attempt':batch(latest),'active':bool(active),'outcome':outcome}
