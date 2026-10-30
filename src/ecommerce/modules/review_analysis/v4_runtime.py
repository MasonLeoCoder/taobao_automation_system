"""新版后台执行和已存返回恢复；任何付费动作仅来自已确认任务，不自动重试。"""
from dataclasses import replace
import copy
import json
from ecommerce.infrastructure.ai.config import load_config
from ecommerce.infrastructure.ai.client import AIError
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.modules.review_analysis import v4


def continuation(data):
    """固定剩余提取和报告最大请求范围；参数：持久聚合；返回：补处理计划。"""
    task=data['task'];done={d['result']['id'] for d in data['details']}
    pending=[s['snapshot'] for s in data['sources'] if s['review_id'] not in done]
    original=task['parameters']['plan']
    groups=v4.plan(pending,task['rules'],original['context'],original['timeout_seconds'])['groups'] if pending else []
    complete=bool(data.get('report') and data['report']['content'].get('narrative') and data['report']['content'].get('detail_revisions')=={d['id']:d['revision'] for d in data['details']})
    slots=0 if complete else original['report_slots']
    return {'version':4,'policy':v4.POLICY,'source_ids':[r['id'] for r in pending],'groups':groups,
        'report_needed':not complete,'report_slots':slots,'required_calls':len(groups)+slots}


def remaining_budget(data):
    """计算继续的保守上限；参数：持久聚合；返回：补处理和额度。"""
    task=data['task'];plan=task['parameters']['plan'];supplement=continuation(data)
    budget=repository.budget_status(task['parameters'],data['calls'])
    cost=sum(g['reserved_tokens'] for g in supplement['groups'])+supplement['report_slots']*plan['report_each_reserved_tokens']
    minimum=budget['used_tokens']+cost
    return {'minimum_tokens':minimum,'required_calls':supplement['required_calls'],'supplement':supplement,
        'plan_hash':fingerprint(supplement),'suggested_tokens':max(task['parameters']['max_tokens'],minimum+max(4096,(cost+9)//10)),
        'suggested_calls':max(task['parameters']['max_calls'],len(data['calls'])+supplement['required_calls'])}


def matching_report(data,index,incoming):
    """只复用同输入的已有返回；参数：数据、阶段序号、输入；返回：局部有效报告或None。"""
    for call in reversed(data['calls']):
        if call['kind']=='report' and call['chunk_index']==index and call['response'] and call['request'].get('report_input')==incoming:
            result=v4.report_assessment(call['response'],incoming)
            if result['compiled'] and result['compiled'].get('schema_version')=='compiled-competitor-report-v1.2':return result
    return None


def build_report(settings,task_id,narrative=None,diagnostics=None,report_input=None):
    """追加本地报告，不联网；参数：任务、有效AI稿、诊断和输入；返回：版本。"""
    data=repository.read(settings,task_id)
    return repository.save_report(settings,task_id,v4.content(data,narrative,diagnostics,report_input))


def recover_saved(settings,task_id):
    """从终态原响应本地恢复和重建报告；参数：配置、任务；返回：恢复数量，不联网。"""
    data=repository.read(settings,task_id);by_id={s['review_id']:s['snapshot'] for s in data['sources']}
    added=[];recorded=False
    for call in data['calls']:
        if call['kind']=='classify' and call['response'] and call['status'] not in ('inflight','unknown'):
            result=v4.assess(call['response']['text'],[by_id[uid] for uid in call['request']['source_ids']],call['response']['finish_reason'])
            outcome=repository.recover_call(settings,task_id,call['id'],result,fingerprint(call['response']))
            added.extend(outcome['added_ids'])
            recorded=recorded or outcome['recorded']
    current=repository.read(settings,task_id);narrative=None;diagnostics=[];incoming=v4.context(current)
    saved=matching_report(current,0,incoming)
    if saved:narrative=saved['compiled'];diagnostics=[saved['structure_issues'],saved['issues'],saved['warnings']]
    if not saved:
        for call in reversed(current['calls']):
            candidate=call['request'].get('report_input')
            if call['kind']!='report' or call['chunk_index']!=0 or not candidate or not candidate.get('segment_findings'):continue
            # 总报告引用必须仍对应当前明细；内容修正后不从旧简报恢复旧结论。
            if call['request'].get('detail_revisions')!={d['id']:d['revision'] for d in current['details']}:continue
            result=matching_report(current,0,candidate)
            if result:narrative=result['compiled'];diagnostics=[result['structure_issues'],result['issues'],result['warnings']];incoming=candidate;break
    if recorded or current['report'] is None or (narrative and not current['report']['content'].get('narrative')):
        build_report(settings,task_id,narrative,diagnostics,incoming)
    count=len(current['details']);complete=count==current['task']['source_count'] and narrative is not None
    repository.update_task(settings,task_id,status='succeeded' if complete else 'partial' if count else 'failed',
        active_key=None,owner=None,message=f'本地校验保留{count}条提取；'+('报告已恢复，仍待复核。' if narrative else '报告尚未生成；未调用AI。'))
    return {'id':task_id,'added_count':len(added),'processed_count':count,'pending_count':current['task']['source_count']-count,'ai_calls':0}


def execute(settings,task_id,events,client_factory):
    """执行已授权轮次并保存全部收据；参数：配置、任务、事件、客户端工厂；返回：终态。"""
    data=repository.read(settings,task_id);task=data['task'];plan=task['parameters']['plan']
    by_id={s['review_id']:s['snapshot'] for s in data['sources']}
    directory=settings.workspace/'output/analysis_receipts'/task_id;directory.mkdir(parents=True,exist_ok=True)
    diagnostics=[];narrative=None;report_input=None;client=None
    # 收据先补写。进程中断不等于可以重发；无收据则记录unknown，等待已确认的补处理。
    for call in data['calls']:
        if call['status']!='inflight':continue
        path=directory/(call['id']+'.json')
        if not path.exists():
            repository.finish_call(settings,task_id,call['id'],'unknown',None,'原进程未确认且没有返回收据，结果和费用未知。');continue
        response=json.loads(path.read_text(encoding='utf-8'))
        if call['kind']=='classify':
            assessment=v4.assess(response['text'],[by_id[uid] for uid in call['request']['source_ids']],response['finish_reason'])
            repository.finish_call(settings,task_id,call['id'],'invalid' if assessment['issues'] else 'succeeded',response,
                                   '含局部问题，合格条目已保留。' if assessment['issues'] else '',assessment=assessment)
        else:
            incoming=call['request'].get('report_input')
            result=v4.report_assessment(response,incoming) if incoming else None
            invalid=not result or not result['compiled'] or result['issues'] or result['structure_issues']
            repository.finish_call(settings,task_id,call['id'],'invalid' if invalid else 'succeeded',response,'报告需复核。' if invalid else '')

    def request(kind,index,incoming,source_ids,output):
        """保存意图后单次发送；参数：用途、序号、资料、身份、输出；返回：校验结果。"""
        nonlocal client
        current=repository.read(settings,task_id)
        if kind=='report':
            old=matching_report(current,index,incoming)
            if old:return old
        text=v4.prompt(task['rules'],kind,incoming)
        if len(text)>v4.MAX_INPUT:raise ValueError('报告或提取资料超出完整发送边界；未截断或发送，已有统计和原文保留。请核对后确认新的处理计划。')
        call_id=repository.begin_call(settings,task_id,kind,index,text,source_ids,v4.reserve(text,output),
            output_tokens=output,json_mode=True,timeout_seconds=plan['timeout_seconds'],report_input=incoming if kind=='report' else None)
        if kind=='report':
            # 内容版本用于阻止人工修订之后恢复旧总报告。
            with repository.connection(settings,True) as (conn,tables):
                row=conn.execute(tables['analysis_calls'].select().where(tables['analysis_calls'].c.id==call_id)).mappings().one()
                payload={**row['request'],'detail_revisions':{d['id']:d['revision'] for d in current['details']}}
                conn.execute(tables['analysis_calls'].update().where(tables['analysis_calls'].c.id==call_id).values(request=payload))
        events.emit('analysis_call_started','开始已确认分析调用',details={'analysis_id':task_id,'call_id':call_id,'kind':kind,'index':index})
        try:
            if client is None:client=client_factory(replace(load_config(settings.root),model=task['model']),provider=task['provider'],model=task['model'])
            response=client.complete(text,max_tokens=output,require_complete=False,json_mode=True,timeout_seconds=plan['timeout_seconds'])
        except AIError:
            repository.finish_call(settings,task_id,call_id,'unknown',None,'接口请求未确认，结果及费用未知；未自动重试。')
            raise ValueError('请求结果未知，已停止后续请求；先核对原始调用再明确是否继续。') from None
        atomic_json(directory/(call_id+'.json'),response)
        if kind=='classify':
            assessed=v4.assess(response['text'],[by_id[uid] for uid in source_ids],response['finish_reason'])
            repository.finish_call(settings,task_id,call_id,'invalid' if assessed['issues'] else 'succeeded',response,
                '含局部问题，有效提取保留。' if assessed['issues'] else '',assessment=assessed)
            events.emit('analysis_call_finished','提取返回已保存，局部问题另列',details={'analysis_id':task_id,'call_id':call_id,'kind':kind})
            if assessed['issues']:diagnostics.append({'stage':'extraction','issues':assessed['issues']})
            return assessed
        assessed=v4.report_assessment(response,incoming)
        valid=assessed['compiled'] and assessed['compiled'].get('schema_version')=='compiled-competitor-report-v1.2'
        bad=not valid or assessed['issues'] or assessed['structure_issues']
        repository.finish_call(settings,task_id,call_id,'invalid' if bad else 'succeeded',response,'报告含局部问题或结构不符，有效部分保留。' if bad else '')
        events.emit('analysis_call_finished','报告返回及诊断已保存',details={'analysis_id':task_id,'call_id':call_id,'kind':kind})
        diagnostics.append({'stage':'report','index':index,'structure':assessed['structure_issues'],'issues':assessed['issues'],'warnings':assessed['warnings']})
        if not valid:raise ValueError('报告返回不能形成有效报告，已保存原文和用量；未自动重试。')
        return assessed

    try:
        current=repository.read(settings,task_id)
        supplement=task['parameters'].get('active_supplement')
        if any(c['status']=='unknown' for c in current['calls']) and not supplement:
            raise ValueError('存在结果未知请求，未继续联网；请先确认重发风险。')
        groups=supplement['groups'] if supplement else plan['groups']
        done={d['result']['id'] for d in current['details']}
        for group in groups:
            missing=[uid for uid in group['source_ids'] if uid not in done]
            if not missing:continue
            if missing!=group['source_ids']:raise ValueError('分组中已有部分有效结果；先本地恢复并确认仅剩余评价的补处理计划。')
            repository.update_task(settings,task_id,message=f'正在提取第{group["index"]}组，已保存{len(done)}条。')
            request('classify',group['index'],{'product_context':plan['context'],'reviews':[by_id[uid] for uid in missing]},missing,group['max_output_tokens'])
            done={d['result']['id'] for d in repository.read(settings,task_id)['details']}
        current=repository.read(settings,task_id);report_input=v4.context(current)
        if not current['details']:raise ValueError('暂无合格提取，原返回保留；未请求报告。')
        if len(v4.prompt(task['rules'],'report',report_input))>v4.MAX_INPUT:
            parts=[];all_rows=v4.rows(current)
            for group in plan['groups']:
                selected=[r for r in all_rows if r['id'] in group['source_ids']]
                if not selected:continue
                part_input=v4.context(current,selected)
                result=request('report',group['index'],part_input,[],plan['report_output_tokens'])
                parts.append(result['compiled'])
            report_input=v4.summary_context(current,parts)
        result=request('report',0,report_input,[],plan['report_output_tokens'])
        narrative=result['compiled']
    except ValueError as error:diagnostics.append({'stage':'execution','message':str(error)})
    build_report(settings,task_id,narrative,diagnostics,report_input)
    count=len(repository.read(settings,task_id)['details'])
    status='succeeded' if count==task['source_count'] and narrative is not None else 'partial' if count else 'failed'
    repository.update_task(settings,task_id,status=status,active_key=None,owner=None,
        message=f'提取已保存{count}/{task["source_count"]}条；'+('报告已生成，关键推断和反证待复核。' if narrative else 'AI报告尚未生成，可先查看本地统计和原文。'))
    return status
