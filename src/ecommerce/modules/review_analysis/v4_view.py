"""新版报告读模型及明确人工修正；不调用AI，不把展示通过当语义确认。"""
import copy
import json
from ecommerce.infrastructure.storage import analysis_store as repository,analysis_review_store
from ecommerce.modules.review_analysis import v4,v4_runtime


def enrich(settings,data,version=None):
    """整理选定报告、复核和来源定位；参数：配置、聚合、可选版本；返回：展示对象。"""
    from ecommerce.modules.review_analysis import service
    if version is not None:
        selected=next((r for r in data['reports'] if r['version']==version),None)
        if selected is None:raise LookupError('报告版本不存在。')
        data['report']=selected
    data['report']=copy.deepcopy(data['report'])
    content=data['report']['content'] if data['report'] else v4.content(data)
    data['v4_content']=content
    data['is_latest_report']=bool(data['report'] and data['reports'] and data['report']['id']==data['reports'][-1]['id'])
    data['report_stale']=content['detail_revisions']!={d['id']:d['revision'] for d in data['details']}
    records=analysis_review_store.decisions(settings,data['task']['id'],data['report']['id']) if data['report'] else []
    data['review_records']=records;items={i['key']:i for i in content['review_items']}
    for record in records:
        item=items.get(record['item_key'])
        if item and record['candidate_hash']==item['candidate_hash'] and record['source_hash']==content['source_hash']:
            item.update(decision=record['decision'],revision=record['revision'],reviewer=record['reviewer'],reason=record['reason'],review_context=record['review_context'])
    data['review_counts']={s:sum(i['decision']==s for i in items.values()) for s in ('pending','adopted','rejected')}
    content['review_status']='reviewed' if items and data['review_counts']['pending']==0 else 'partially_reviewed' if any(i['decision']!='pending' for i in items.values()) else 'unreviewed'
    data['review_by_key']=items
    data['review_disabled']=not data['is_latest_report'] or data['report_stale'] or data['task']['status'] in ('queued','running')
    data['needs_local_recovery']=service.needs_local_recovery(data)
    try:data['resume_budget']=v4_runtime.remaining_budget(data)
    except ValueError as error:data['resume_budget']={'blocked':str(error),'required_calls':0}
    data['outgoing']=data['task']['parameters']['plan']['groups'];data['validation_issues']=[];data['normalizations']=[]
    effective=content.get('extraction_snapshot',v4.rows(data));data['extracted_rows']=effective
    data['stat_by_id']={s['id']:s for s in content['statistics']}
    for stat in content['statistics']:
        stat['sentiment_views']=[{'name':s,'count':stat['sentiments'].get(s,{}).get('count',0),'ids':stat['sentiments'].get(s,{}).get('review_ids',[])} for s in ('正面','负面','中性','褒贬并存','不明确')]
    narrative=content.get('narrative')
    if narrative:
        seen={}
        for finding in narrative['findings']:
            signature=tuple(sorted({(r['review_id'],r['observation_id']) for f in finding['facts'] for r in f['observation_refs']}))
            finding['canonical_id']=seen.setdefault(signature,finding['id'])
        for card in narrative['opportunities']:
            own=[i for i in items.values() if i['card_id']==card['id']]
            proposal=items.get('opportunity:'+card['id'])
            card['decision']='rejected' if proposal and proposal['decision']=='rejected' else 'adopted' if proposal and proposal['decision']=='adopted' and all(i['decision']!='pending' for i in own) else 'pending'
    for detail in data['details']:
        detail['v4_points']=v4.effective(detail)['observations']
    return data


def decide(settings,task_id,**values):
    """保存用户明确选择；参数：任务及绑定身份、意见；返回：新复核序号。"""
    return analysis_review_store.save(settings,task_id,**values)


def correct(settings,task_id,detail_id,*,expected_revision,points,excluded,reason):
    """在来源依据不变的前提下修正观点；参数：明细、版本、保留观点、排除、原因；返回：本地报告版本。"""
    if type(expected_revision) is not int or expected_revision<0 or type(excluded) is not bool or not isinstance(reason,str) or not 1<=len(reason.strip())<=500:
        raise ValueError('修正版本、排除选项或原因无效。')
    data=repository.read(settings,task_id)
    if data['task']['rule_version']!=v4.VERSION:raise ValueError('旧任务须使用原修正入口。')
    detail=next((d for d in data['details'] if d['id']==detail_id),None)
    if detail is None:raise LookupError('分析明细不存在。')
    value=v4.effective(detail);original={o['observation_id']:o for o in value['observations']}
    if not isinstance(points,list):raise ValueError('观点修正必须为数组。')
    if excluded and not points:
        value['excluded']=True
        repository.correct(settings,task_id,detail_id,expected_revision,value,reason.strip())
        return {'version':v4_runtime.build_report(settings,task_id,diagnostics=[{'stage':'manual','message':'整条按人工意见排除，原提取及依据保留；未调用AI。'}]),'ai_calls':0}
    ids=[];updated=[]
    for point in points:
        if not isinstance(point,dict) or set(point)!={'observation_id','topic','sentiment','fact_text'}:raise ValueError('观点修正字段无效。')
        oid=point['observation_id']
        if oid not in original or oid in ids:raise ValueError('观点编号无效或重复。')
        ids.append(oid);row=copy.deepcopy(original[oid]);changed={k for k in ('topic','sentiment','fact_text') if row[k]!=point[k]}
        for name in ('topic','sentiment','fact_text'):
            if not isinstance(point[name],str) or not 1<=len(point[name].strip())<=500:raise ValueError('观点主题、概括或倾向无效。')
            row[name]=point[name]
        row['uncertainties']=[{**u,'fields':[f for f in u['fields'] if f not in changed]} for u in row['uncertainties'] if set(u['fields'])-changed]
        row.pop('eligibility',None);updated.append(row)
    value['observations']=updated
    value['relations']=[r for r in value['relations'] if set(r['observation_ids'])<=set(ids)]
    clean={key:value[key] for key in v4.contract.REVIEW['properties']}
    source=next(s['snapshot'] for s in data['sources'] if s['id']==detail['source_id'])
    assessment=v4.assess(json.dumps({'schema_version':'reviews-extraction-v1.2','reviews':[clean]},ensure_ascii=False),[source])
    if assessment['issues'] or len(assessment['results'])!=1:raise ValueError('修正后提取未通过结构和原文校验，请保留有效依据或使用整条排除。')
    result=assessment['results'][0];result['excluded']=excluded
    repository.correct(settings,task_id,detail_id,expected_revision,result,reason.strip())
    version=v4_runtime.build_report(settings,task_id,diagnostics=[{'stage':'manual','message':'已按明确人工修正重新统计；旧综合推断不自动沿用，未调用AI。'}])
    return {'version':version,'ai_calls':0}
