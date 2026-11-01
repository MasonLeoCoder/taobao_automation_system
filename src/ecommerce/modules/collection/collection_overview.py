"""当前商品九模块的只读采集管理摘要，不继承历史批次或搜索运行限制。"""
from ecommerce.modules.collection.product_view import module_summaries
from ecommerce.modules.collection.web_collection import MODULES,remaining_plan
from datetime import datetime
from ecommerce.infrastructure.storage.time_values import iso_value

LABELS=dict(zip(MODULES,('基本信息','视频','主图','SKU','商品参数','详情图片','评价概览','评价明细','问大家')))
STATES={'complete':'已完成','downloaded':'已完成','no_video':'已确认无内容','no_images':'已确认无内容',
        'no_parameters':'已确认无内容','no_reviews':'已确认无内容','no_questions':'已确认无内容','skipped':'已确认无入口',
        'partial':'部分完成','failed':'采集失败','failed_skipped':'失败，已跳过','not_run_skipped':'未执行',
        'not_run':'未执行','cancelled':'已停止','interrupted':'已中断','needs_attention':'待核对','unverified':'状态待核实',
        'running':'采集中','waiting':'等待执行','queued':'等待执行','paused':'队列已暂停','starting':'启动中','stopping':'正在停止','storage_pending':'等待入库',
        'discarded':'未完成明细已清理','resolved':'已补采解决','complete_with_errors':'已结束，存在错误','complete_with_warnings':'已结束，含警告'}
BUSY={'running','waiting','queued','paused','starting','stopping','storage_pending','unverified'}
ISSUES={'partial','failed','failed_skipped','not_run_skipped','not_run','cancelled','interrupted','needs_attention','unverified','discarded','complete_with_errors'}


def _stamp(value):
    """统一来自数据库或公开查询的北京时间排序键；参数：时刻；返回：ISO或空串。"""
    return iso_value(value) if isinstance(value,datetime) else value or ''


def build(data,states=(),*,runtime_error=False):
    """合并有效资料、最近步骤和现有申请；参数：商品查询及公开任务；返回：固定顺序九行。"""
    summaries=module_summaries(data,data['history']);remaining=set(remaining_plan(data)['modules']);rows=[]
    for code in MODULES:
        item=summaries[code];valid=item['valid'];latest=item['latest'];attempt=(data.get('module_attempts') or {}).get(code)
        if latest and (not attempt or (latest.get('run_id')!=attempt['run_id'] and _stamp(latest.get('captured_at'))>_stamp(attempt.get('at')))):
            attempt={'state':latest['status'],'task_id':latest.get('task_id'),'run_id':latest.get('run_id'),
                     'at':latest.get('captured_at'),'batch_id':latest['id'],'message':''}
        attempt=dict(attempt or {})
        related=[job for job in states if job.get('product_id')==data['product']['platform_product_id'] and (job.get('module')==code or
                    job.get('mode') in {'detail','remaining'} and code in job.get('modules',[]))]
        active=next((job for job in related if not job['done']),None)
        newest=max(related,key=lambda job:_stamp(job.get('created_at')),default=None)
        # 启动失败等可能没有步骤或批次；同任务的具体模块结果仍优先于批量任务的整体失败。
        if not active and newest and newest['done'] and newest['state'] in ISSUES and not (newest.get('task_id') and newest['task_id']==attempt.get('task_id')) and _stamp(newest.get('created_at'))>_stamp(attempt.get('at')):
            attempt={'state':newest['state'],'task_id':newest.get('task_id'),'at':newest.get('created_at'),'message':newest.get('message','')}
        if active:
            attempt.update(state=active['state'],task_id=active.get('task_id'),job_id=active['id'],at=active.get('created_at'),message=active.get('message',''))
        state=attempt.get('state');busy=bool(active or state in BUSY)
        no_entry=code in data.get('no_entry_modules',[]) and not valid
        saved='已确认无内容' if valid and valid['status'].startswith('no_') else '已有资料' if valid else '已确认无入口' if no_entry else '暂无有效资料' if latest or attempt else '未采集'
        count=item['count'];units={'video':'个视频','main_images':'张图片','skus':'种规格','parameters':'项参数','detail_images':'张图片','review_summary':'个标签','reviews':'条评价','questions':'个问题'}
        quantity=('已保存基本信息' if code=='basic_info' else f"已保存 {count} {units[code]}") if valid else '没有有效资料记录'
        same=bool(valid and not active and state in {'complete','downloaded','no_video','no_images','no_parameters','no_reviews','no_questions'} and
                  ((attempt.get('run_id') and attempt['run_id']==valid.get('run_id')) or attempt.get('batch_id')==valid['id']))
        media=data.get('missing_media') or {}
        missing=media.get(str(valid['id']),media.get(valid['id'],0)) if valid else 0
        message=str(attempt.get('message') or '') if state in ISSUES or busy else ''
        rows.append({'module':code,'label':LABELS[code],'has_valid':bool(valid),'saved_state':saved,'quantity':quantity,
                     'source_batch':valid['id'] if valid else None,'source_task':valid.get('task_id') if valid else None,
                     'captured_at':valid.get('captured_at') if valid else None,'scope_batch':valid,
                     'attempt_state':state,'attempt_label':'与有效资料一致' if same else STATES.get(state,state or '尚未尝试'),
                     'attempt_at':None if same else attempt.get('at'),'attempt_task':attempt.get('task_id'),
                     'message':message[:100]+('…' if len(message)>100 else ''),'busy':busy,'remaining':code in remaining,
                     'issue':state in ISSUES,'runtime_error':runtime_error,
                     'collect_label':'重新采集' if valid or attempt or no_entry else '采集',
                     'media_note':f'有 {missing} 条素材记录未关联可用文件' if missing else '素材可用性请进入资料页核对' if code in {'video','main_images','detail_images','skus'} and valid and not valid['status'].startswith('no_') else '',
                     'default_range':'默认最多100条；修改数量请选范围' if code=='reviews' else '默认最多50题；修改数量请选范围' if code=='questions' else '',
                     'active_job':active['id'] if active else None})
    return {'rows':rows,'ready':sum(r['has_valid'] for r in rows),'remaining':len(remaining),
            'issues':sum(r['issue'] for r in rows),'runtime_error':runtime_error}
