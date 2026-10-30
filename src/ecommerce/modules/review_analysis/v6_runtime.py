"""新直接报告执行：固定消息、可核对收据、明确补处理、零自动付费重试。"""
import json
from dataclasses import replace
from ecommerce.infrastructure.ai.client import AIError
from ecommerce.infrastructure.ai.config import load_config
from ecommerce.infrastructure.ai.capabilities import Counter
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.modules.review_analysis import v6,v6_report


def cached(data,index,values):
    """同用途序号、相同真实消息、完整原响应才复用；参数：聚合、序号、消息；返回：调用或None。"""
    digest=fingerprint(values)
    return next((c for c in reversed(data['calls']) if c['kind']=='report' and c['chunk_index']==index
        and c['request'].get('request_hash')==digest and c['request'].get('messages')==values and c['response'] and c['response']['finish_reason']=='stop'
        and isinstance(c['response'].get('text'),str) and c['response']['text'].strip()
        and (index!=0 or v6_report.check(c['response']['text'],c['response']['finish_reason'],[s['snapshot'] for s in data['sources']])['complete'])),None)


def completed(data):
    """找出当前固定分组稿和总稿；参数：聚合；返回：完成组及总调用。"""
    plan=data['task']['parameters']['plan'];groups=[]
    for group in plan['groups']:
        value=cached(data,group['index'],group['messages'])
        if value:groups.append(value)
    final=None
    if plan['mode']=='direct' and groups:final=groups[0]
    elif plan['mode']=='grouped' and len(groups)==len(plan['groups']):final=cached(data,0,v6.summary_messages(data['task'],groups))
    return groups,final


def continuation(data):
    """列出仅未完成请求，原分组不改；参数：聚合；返回：补处理计划。"""
    plan=data['task']['parameters']['plan'];done,final=completed(data)
    indices={c['chunk_index'] for c in done};pending=[g for g in plan['groups'] if g['index'] not in indices]
    summary=bool(plan['summary'] and not final)
    return {'version':6,'groups':pending,'source_ids':list(dict.fromkeys(uid for g in pending for uid in g['source_ids'])),
        'report_needed':bool(pending or summary),'report_slots':int(summary),'required_calls':len(pending)+int(summary),
        'completed_call_ids':[c['id'] for c in done],'plan_hash':fingerprint(plan)}


def remaining_budget(data):
    """包含已有未知预留，不自动增加额度；参数：聚合；返回：所需额度。"""
    supplement=continuation(data);plan=data['task']['parameters']['plan'];parameters=data['task']['parameters']
    cost=sum(g['reserved_tokens'] for g in supplement['groups'])+(plan['summary']['reserved_tokens'] if supplement['report_slots'] else 0)
    used=repository.budget_status(parameters,data['calls'])['used_tokens'];minimum=used+cost
    return {'minimum_tokens':minimum,'required_calls':supplement['required_calls'],'supplement':supplement,'plan_hash':fingerprint(supplement),
        'suggested_tokens':max(parameters['max_tokens'],minimum),'suggested_calls':max(parameters['max_calls'],len(data['calls'])+supplement['required_calls'])}


def build_report(settings,task_id,issues=None):
    """从持久返回本地重建并检查，不调用AI；参数：配置、任务、说明；返回：版本。"""
    data=repository.read(settings,task_id);groups,final=completed(data);plan=data['task']['parameters']['plan']
    phases=[{'index':c['chunk_index'],'review_ids':c['request']['source_ids'],'text':c['response']['text']} for c in groups] if plan['mode']=='grouped' else []
    if final is None:
        # 截断正文也保留可读，但不能据此复用为完整结果。
        last=next((c for c in reversed(data['calls']) if c['chunk_index']==0 and c['response']),None)
        if last:final=last
    if plan['mode']=='local_empty':
        response={'text':'\n\n'.join('## '+s+'\n本批评价没有可分析的文字，资料不足。' for s in v6_report.SECTIONS),'finish_reason':'stop'}
        return repository.save_report(settings,task_id,v6_report.content(data,response,issues=issues,local=True))
    return repository.save_report(settings,task_id,v6_report.content(data,final['response'] if final else None,phases,issues))


def recover_saved(settings,task_id,*,update_state=True):
    """仅补写本任务已有收据并检查显示；参数：配置、任务；返回：恢复摘要，不联网。"""
    data=repository.read(settings,task_id);directory=settings.workspace/'output/analysis_receipts'/task_id
    for call in data['calls']:
        if call['status']!='inflight':continue
        path=directory/(call['id']+'.json')
        if path.exists():
            response=json.loads(path.read_text(encoding='utf-8'));good=response.get('finish_reason')=='stop' and bool(response.get('text','').strip())
            repository.finish_call(settings,task_id,call['id'],'succeeded' if good else 'invalid',response,'已有收据本地补写，未联网。')
        else:repository.finish_call(settings,task_id,call['id'],'unknown',None,'没有可恢复响应，结果及用量未知，未重发。')
    version=build_report(settings,task_id,['已核对本任务保存的原响应；未调用AI。'])
    if update_state:
        current=repository.read(settings,task_id);report=current['report']['content']
        state='succeeded' if report['validation']['complete'] else 'partial' if report['markdown'] or report['phases'] else 'failed'
        repository.update_task(settings,task_id,status=state,owner=None,active_key=None,message='已有返回已本地核对，未重新请求AI；原文和用量保留。')
    return {'id':task_id,'version':version,'ai_calls':0,'added_count':0}


def execute(settings,task_id,events,client_factory):
    """执行已经确认的固定计划；参数：配置、任务、事件、客户端工厂；返回：终态。"""
    data=repository.read(settings,task_id);task=data['task'];plan=task['parameters']['plan']
    profile=plan['capability'];config=replace(load_config(settings.root),model=task['model']);counter=Counter(settings.root,config,profile)
    directory=settings.workspace/'output/analysis_receipts'/task_id;directory.mkdir(parents=True,exist_ok=True)
    client=None;issues=[]
    if any(c['status']=='inflight' for c in data['calls']):recover_saved(settings,task_id,update_state=False)

    def request(index,values,ids,output):
        """联网前保存确切消息与额度；参数：序号、消息、来源、输出；返回：响应，不重试。"""
        nonlocal client
        current=repository.read(settings,task_id);old=cached(current,index,values)
        if old:return old['response']
        ok,size=v6.capacity_ok(counter,profile,values,output)
        if not ok:raise ValueError('当前实际请求超过冻结容量或网络保护，未发送；已有分组结果保留，不自动增加层级。')
        call_id=repository.begin_call(settings,task_id,'report',index,v6.prompt_view(values),ids,size['reserved_tokens'],
            output_tokens=output,json_mode=False,timeout_seconds=plan['timeout_seconds'],messages=values,measurement=size)
        events.emit('analysis_call_started','开始已确认报告请求',details={'analysis_id':task_id,'call_id':call_id,'kind':'report','index':index})
        try:
            if client is None:client=client_factory(config,provider=task['provider'],model=task['model'])
            response=client.complete_messages(values,max_tokens=output,capability=profile,timeout_seconds=plan['timeout_seconds'])
        except AIError as error:
            message=str(error)+' 结果或用量尚未确认；未自动重试。'
            repository.finish_call(settings,task_id,call_id,'unknown',None,message)
            events.emit('analysis_call_finished',message,details={'analysis_id':task_id,'call_id':call_id,'status':'unknown'})
            raise ValueError(message) from None
        atomic_json(directory/(call_id+'.json'),response)
        good=response.get('finish_reason')=='stop' and isinstance(response.get('text'),str) and bool(response['text'].strip())
        message='' if good else '响应未完整结束或没有可读内容，原返回与用量已保存；未自动重试。'
        if good and index==0 and not v6_report.check(response['text'],response['finish_reason'],[s['snapshot'] for s in current['sources']])['complete']:
            good=False;message='报告缺少必要章节或有效原文引用，可读内容已保留；未自动付费补写。'
        repository.finish_call(settings,task_id,call_id,'succeeded' if good else 'invalid',response,message)
        events.emit('analysis_call_finished','报告原返回及用量已保存',details={'analysis_id':task_id,'call_id':call_id,'status':'succeeded' if good else 'invalid'})
        if not good:raise ValueError(message)
        return response

    try:
        current=repository.read(settings,task_id)
        if any(c['status']=='unknown' for c in current['calls']) and not task['parameters'].get('active_supplement'):
            raise ValueError('存在结果未知请求；先核对原调用并明确确认风险，未继续联网。')
        if plan['mode']=='local_empty':
            build_report(settings,task_id)
        else:
            for group in plan['groups']:
                repository.update_task(settings,task_id,message='正在生成详细报告。' if group['kind']=='direct' else f'正在整理分组{group["index"]}/{len(plan["groups"])}，已完成内容持续保存。')
                request(group['index'],group['messages'],group['source_ids'],group['max_output_tokens'])
                if group['kind']=='group':build_report(settings,task_id)
            if plan['summary']:
                current=repository.read(settings,task_id);done,_=completed(current)
                values=v6.summary_messages(task,done)
                repository.update_task(settings,task_id,message='分组要点已保存，正在汇总详细报告。')
                request(0,values,plan['source_ids'],plan['report_output_tokens'])
            build_report(settings,task_id)
    except ValueError as error:
        issues.append(str(error));build_report(settings,task_id,issues)
    current=repository.read(settings,task_id);report=current['report']['content'];complete=report['validation']['complete']
    status='succeeded' if complete else 'partial' if report['markdown'] or report['phases'] else 'failed'
    message='报告已生成；'+('本地完整性及引用检查通过，分析仍待人工核对。' if report['validation']['state']=='complete' else '报告可阅读，存在需要核对的引用或章节。') if complete else '报告生成未完成，已保存可读内容及原返回。'
    if issues:message+=' 原因：'+issues[-1]
    if plan['mode']=='local_empty':message='本批评价没有可分析的文字；已保存本地资料不足说明，未调用AI。'
    repository.update_task(settings,task_id,status=status,active_key=None,owner=None,message=message)
    return status
