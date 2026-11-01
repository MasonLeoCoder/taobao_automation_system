"""九模块更新的只读概况和范围规则，不触发采集。"""
from ecommerce.infrastructure.storage.time_values import iso_value
from ecommerce.shared.collection_contract import MODULES

LABELS=dict(zip(MODULES,('基本信息','视频','主图','SKU信息','商品参数','详情图片','评价概览','评价明细','问大家')))
EMPTY={'video':{'no_video'},'main_images':{'no_images'},'parameters':{'no_parameters'},
       'detail_images':{'no_images'},'review_summary':{'no_reviews'},'reviews':{'no_reviews'},'questions':{'no_questions'}}
STATES={'queued':'等待执行','paused':'队列已暂停','running':'采集中','starting':'启动中','stopping':'正在停止',
        'needs_attention':'待人工处理','storage_pending':'等待入库','failed':'更新失败','cancelled':'已停止',
        'interrupted':'已中断','partial':'部分完成'}

def stamp(value):
    """统一比较时间；参数：时间值；返回：可排序字符串。"""
    return iso_value(value) if value and not isinstance(value,str) else value or ''


def build(data, states, module):
    """整理独立模块的有效资料及申请；参数：商品、公开任务、模块；返回：只读概况。"""
    if module not in MODULES:raise ValueError('不支持的更新模块')
    rows=[r for r in data['history'] if r['module']==module]
    current=set(data['current_ids'])
    def valid(row):
        return row['id'] in current and (row['status'] in EMPTY.get(module,set()) or
            row['status'] in {'complete','downloaded'} and (module in {'basic_info','review_summary'} or row.get('record_counts',{}).get('records',0)>0))
    def batch(row):
        return {**{k:row.get(k) for k in ('id','status','captured_at','task_id')},'count':row.get('record_counts',{}).get('records',0)} if row else None
    effective=next((r for r in rows if valid(r)),None)
    related=sorted((j for j in states if j.get('product_id')==data['product']['platform_product_id'] and
        (j.get('module')==module or module in (j.get('modules') or []))),key=lambda j:(stamp(j.get('created_at')),j['id']),reverse=True)
    active=next((j for j in related if not j['done']),None)
    job=active or next(iter(related),None);latest=next(iter(rows),None)
    outcome=None
    newer=job and (not latest or (job.get('task_id') or job['id'])!=latest.get('task_id') and stamp(job.get('created_at'))>stamp(latest.get('captured_at')))
    if active or newer:
        result=next((r for r in job.get('results',[]) if r.get('module')==module),{})
        skipped=bool(result.get('skipped'))
        outcome={'label':'未找到入口' if skipped else STATES.get(job['state'],'结果待核对'),
            'message':job.get('message',''),'complete':False,'batch':None,'task_id':job.get('task_id')}
        if latest and latest.get('task_id')==(job.get('task_id') or job['id']) and valid(latest):
            outcome.update(label='本模块已完成，关联任务仍在进行' if active else '更新完成',complete=True,batch=batch(latest))
    elif latest:
        complete=valid(latest)
        outcome={'label':'已确认无内容' if complete and latest['status'] in EMPTY.get(module,set()) else '更新完成' if complete else STATES.get(latest['status'],'结果待核对') if latest['status'] not in {'running','starting'} else '结果待核对',
            'message':'' if complete else '本次未形成完整有效资料。'+('此前有效资料仍保留。' if effective else ''),
            'complete':complete,'batch':batch(latest),'task_id':latest.get('task_id')}
    attempt=(data.get('module_attempts') or {}).get(module) or {}
    if not active and attempt.get('state')=='skipped' and (not latest or stamp(attempt.get('at'))>stamp(latest.get('captured_at'))):
        outcome={'label':'未找到入口','message':attempt.get('message','本次未找到入口，保留已有资料。'),
            'complete':False,'batch':None,'task_id':attempt.get('task_id')}
    options=(job or {}).get('options') or {}
    scope=('不限数量' if options.get('all_reviews' if module=='reviews' else 'all_questions') else
        '默认上限100条有效评价' if module=='reviews' else '默认上限50个问题') if module in {'reviews','questions'} else '本商品对应模块'
    return {'module':module,'label':LABELS[module],'latest_valid':batch(effective),'latest_attempt':batch(latest),
        'active':bool(active),'scope':scope,'outcome':outcome}
