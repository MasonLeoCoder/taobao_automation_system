"""新任务的有界报告输入、同源分片归并及明确排除；旧V4行为保留。"""
import copy
import json
from collections import defaultdict
from ecommerce.modules.review_analysis import v4

VERSION='reviews-v5'
POLICY='reviews-validation-v5.1'
REPORT_VERSION='reviews-report-v5'
MAX_INPUT=v4.MAX_INPUT
EXTRACT_OUTPUT=v4.EXTRACT_OUTPUT
REPORT_OUTPUT=v4.REPORT_OUTPUT
MAX_TOKENS=v4.MAX_TOKENS
contract=v4.contract
process=v4.process
encode=v4.encode
reserve=v4.reserve
effective=v4.effective
rows=v4.rows


def snapshot():
    """固定新任务程序版本，沿用已批准提示；参数：无；返回：规则。"""
    value=v4.snapshot()
    value.update(schema_version=5,version=VERSION,validation_policy=POLICY,wire_format='review-report-wire-v1',allow_exclusions=True)
    return value


def wire(incoming):
    """编码全部观点和依据，消除重复键及统计定位元数据；参数：后台报告输入；返回：实际发送视图。"""
    types=list(contract.TYPES);sentiments=['正面','负面','中性','褒贬并存','不明确']
    targets=['产品','包装','物流','客服','不明确'];sources=['使用体验自述','收货或外观感受','期望','转述','不明确'];events=['当前购买','历史购买','不明确']
    observations=[];evidence=[];details=[];contexts=[]
    for row in incoming['extraction']['reviews']:
        for e in row['evidence']:evidence.append([row['id'],e['evidence_id'],e['segment'],e['quote']])
        for o in row['observations']:
            observations.append([row['id'],o['observation_id'],[types.index(t) for t in o['information_types']],o['topic'],o['fact_text'],
                sentiments.index(o['sentiment']),targets.index(o['target']),sources.index(o['source_type']),events.index(o['event_scope']),
                o['evidence_ids'],o['eligibility']['basic_fact']])
            comparison=o.get('comparison')
            comparison=[comparison[k] for k in ('reference_object','dimension','direction','description','evidence_ids')] if comparison else None
            extra=[o.get('target_detail'),comparison,o.get('explicit_impact'),[[u['fields'],u['reason'],u['evidence_ids']] for u in o.get('uncertainties',[])]]
            if any(extra):details.append([row['id'],o['observation_id'],*extra])
        usage=[[u[k] for k in ('segment','stage','duration_text','evidence_ids')] for u in row['usage_stages']]
        relations=[[r[k] for k in ('relation_type','observation_ids','description','evidence_ids')] for r in row['relations']]
        flags=[[f[k] for k in ('code','message','evidence_ids')] for f in row['flags']]
        if usage or relations or flags:contexts.append([row['id'],usage,relations,flags])
    stats=[]
    for stat in incoming['statistics']:
        counts=stat.get('sentiments',{})
        stats.append([stat['id'],['主题','信息类型'].index(stat['kind']),stat['label'],events.index(stat['event_scope']),stat['count'],
                      [counts.get(s,{}).get('count',0) for s in sentiments]])
    parts=[]
    for index,part in enumerate(incoming.get('segment_findings',[]),1):
        parts.append({'segment_id':index,'facts':[[f['id'],a['fact_id'],a['text'],a['observation_refs'],a['evidence_refs']]
                      for f in part['findings'] for a in f['facts']]})
    return {'wire_format':'review-report-wire-v1',
        'format_note':'下列行数组按columns逐列读取，*_values为从0开始的代码表。输出仍严格使用完整机器结构和原评价/观点/依据编号。extra仅省略null/空数组；所有观点文字和逐字引文均保留。统计的重复来源列表保存在服务端，非新买家数。不同segment_id内同名发现不可混用。',
        'scope':incoming['scope'],'segment_scope':incoming.get('segment_scope'), 'product_context':incoming['product_context'],
        'information_type_values':types,'sentiment_values':sentiments,'target_values':targets,'source_values':sources,'event_values':events,
        'observation_columns':['review_id','observation_id','information_types','topic','fact_text','sentiment','target','source_type','event_scope','evidence_ids','basic_fact'],
        'observations':observations,'observation_extra':details,'review_context':contexts,
        'extra_columns':['review_id','observation_id','target_detail','comparison','explicit_impact','uncertainties'],
        'comparison_columns':['reference_object','dimension','direction','description','evidence_ids'],
        'uncertainty_columns':['fields','reason','evidence_ids'],
        'review_context_columns':['review_id','usage_stages','relations','flags'],
        'usage_columns':['segment','stage','duration_text','evidence_ids'],
        'relation_columns':['relation_type','observation_ids','description','evidence_ids'],'flag_columns':['code','message','evidence_ids'],
        'evidence_columns':['review_id','evidence_id','segment','quote'],'evidence':evidence,
        'statistic_kind_values':['主题','信息类型'],'statistic_columns':['id','kind','label','event_scope','count','sentiment_counts'],
        'statistics':stats,'pending_issues':incoming['pending_issues'],'excluded_sources':incoming.get('excluded_sources',[]),
        'materials':incoming.get('materials',[]),'segment_facts':parts,'basis_coverage':incoming.get('basis_coverage'),
        'projection_format':incoming.get('projection_format')}


def prompt(rules,kind,incoming):
    """生成含输出契约的实际完整文字；参数：规则、用途、输入；返回：发送文字。"""
    return v4.prompt(rules,kind,wire(incoming) if kind=='report' else incoming)


def plan(items,rules,context,timeout):
    """保留固定提取和最大次数，不把超长单条截断；参数：来源及规则；返回：新计划。"""
    value=v4.plan(items,rules,context,timeout)
    value.update(version=5,wire_format='review-report-wire-v1',allow_exclusions=True,
        report_strategy='先以紧凑全量观点和原文直接汇总；仅超限才分段，最终只携带必要事实，不重带完整分段报告。仍超限时保留可读阶段结果，不自动加层或加请求。')
    return value


def merge_fragments(group,source):
    """只归并单独校验合格的同源片段；参数：重复结果、来源；返回：归并对象及引用映射，不能合并则None。"""
    for row in group:
        result=process.extraction({'schema_version':'reviews-extraction-v1.2','reviews':[row]},[source])
        if result['structure_issues'] or result['issues'] or not result['reviews'] or result['reviews'][0]['processing_status']=='pending':return None
    merged={'id':source['id'],**{k:[] for k in ('evidence','usage_stages','observations','relations','flags')}}
    evidence_keys={};mappings=[]
    for index,row in enumerate(group):
        emap={};omap={}
        for e in row['evidence']:
            key=(e['segment'],e['quote'])
            if key not in evidence_keys:
                eid='e'+str(len(evidence_keys)+1);evidence_keys[key]=eid;merged['evidence'].append({**copy.deepcopy(e),'evidence_id':eid})
            emap[e['evidence_id']]=evidence_keys[key]
        def map_e(item):
            """更新当前片段的证据引用，不改变文字；参数：结构对象；返回：副本。"""
            value=copy.deepcopy(item)
            if isinstance(value,dict):
                for key,sub in value.items():value[key]=[emap[e] for e in sub] if key=='evidence_ids' else map_e(sub)
            elif isinstance(value,list):value=[map_e(v) for v in value]
            return value
        for o in row['observations']:
            value=map_e(o);oid='o'+str(len(merged['observations'])+1);omap[o['observation_id']]=oid
            value['observation_id']=oid;merged['observations'].append(value)
        for key in ('usage_stages','flags'):
            for item in row[key]:merged[key].append(map_e(item))
        for relation in row['relations']:
            value=map_e(relation);value['observation_ids']=[omap[o] for o in value['observation_ids']];merged['relations'].append(value)
        mappings.append({'fragment_index':index,'evidence_ids':emap,'observation_ids':omap})
    checked=process.extraction({'schema_version':'reviews-extraction-v1.2','reviews':[merged]},[source])
    return (merged,mappings) if not checked['structure_issues'] and not checked['issues'] else None


def assess(text,sources,finish_reason='stop'):
    """校验新返回、归并安全片段、分类排除与执行故障；参数：返回、来源、结束状态；返回：审计。"""
    value,errors=process.decode(text,finish_reason)
    if value is None or value.get('schema_version')!='reviews-extraction-v1.2' or not isinstance(value.get('reviews'),list):
        result=v4.assess(text,sources,finish_reason);result.update(policy=POLICY,outcomes=[],technical_failure=True);return result
    value=copy.deepcopy(value);lookup={s['id']:s for s in sources};groups=defaultdict(list)
    for row in value['reviews']:
        if isinstance(row,dict) and type(row.get('id')) is int and row['id'] in lookup:groups[row['id']].append(row)
    adjustments=[];replaced=set();normalized=[];recovered=set()
    for row in value['reviews']:
        uid=row.get('id') if isinstance(row,dict) else None
        if type(uid) is int and uid in groups and len(groups[uid])>1:
            if uid in replaced:continue
            replaced.add(uid);merged=merge_fragments(groups[uid],lookup[uid])
            if merged:
                normalized.append(merged[0]);recovered.add(uid)
                adjustments.append({'review_id':uid,'path':'reviews','before':{'fragment_count':len(groups[uid])},'after':{'fragment_count':1},
                    'reason':'各片段独立通过同源逐字引文和结构校验；只统一引用编号，不改原返回、原文或观点含义。','reference_maps':merged[1]})
            else:normalized.extend(groups[uid])
        else:normalized.append(row)
    value['reviews']=normalized
    result=v4.assess(json.dumps(value,ensure_ascii=False),sources,finish_reason)
    result['policy']=POLICY;result['adjustments']=adjustments+result['adjustments'];result['technical_failure']=False
    by_id={r['id']:r for r in result['results']};outcomes=[]
    for source in sources:
        uid=source['id'];row=by_id.get(uid)
        if row:
            row['schema_version']='reviews-detail-v5'
            state='no_information' if row['information_status'] in ('empty','general_only') else 'partial' if row['local_issues'] else 'valid'
            reason='已处理，无具体选品信息。' if state=='no_information' else '保留有效提取，局部观点问题另列。' if state=='partial' else '结构及引文可追溯，语义仍待复核。'
            outcomes.append({'review_id':uid,'state':state,'reason':reason,'merged_fragments':uid in recovered})
        else:outcomes.append({'review_id':uid,'state':'excluded','reason':'本次AI返回未形成可可靠利用的独立条目；本次跳过，不能据此判断原评价无价值。','merged_fragments':False})
    result['outcomes']=outcomes
    return result


def outcomes(data):
    """读取新任务已审计的逐来源状态；参数：持久聚合；返回：编号映射。"""
    result={}
    for record in data['task']['parameters'].get('validation_history',[]):
        if record['policy']==POLICY:
            for item in record.get('outcomes',[]):result[item['review_id']]=copy.deepcopy(item)
    for detail in data['details']:
        row=effective(detail);uid=row['id']
        if detail['corrections']:result[uid]={'review_id':uid,'state':'excluded' if row.get('excluded') else 'no_information' if row['information_status'] in ('empty','general_only') else 'partial' if row.get('local_issues') else 'valid','reason':'以当前明确人工修订为准。','merged_fragments':False}
    return result


def excluded_ids(data):
    """取得本任务明确跳过的来源；参数：持久聚合；返回：编号集合。"""
    return {uid for uid,item in outcomes(data).items() if item['state']=='excluded'}


def context(data,selected_rows=None,parts=None):
    """区分整批覆盖和分段范围；参数：聚合、可选分段、候选发现；返回：后台完整输入。"""
    current=rows(data);selected=current if selected_rows is None else selected_rows
    value=v4.context(data,selected,[]);all_done={r['id'] for r in current};skipped=excluded_ids(data)
    pending=[s['review_id'] for s in data['sources'] if s['review_id'] not in all_done|skipped]
    value['scope'].update(analyzed_count=len(all_done),pending_count=len(pending),excluded_count=len(skipped),
        report_stage='整批报告' if selected_rows is None else '分段报告',
        limitations=['只代表本批有效采集样本，不代表平台全部评价、市场需求或独立买家。','排除内容可能影响结论，不能把未纳入当没有问题；原文及排除原因保留。'])
    value['pending_issues']=[{'review_id':uid,'issue':'该评价尚未形成结果，属于执行未完成，不能当无信息或自动排除。'} for uid in pending]
    ids={r['id'] for r in selected}
    value['segment_scope']={'included_count':len(ids),'review_ids':sorted(ids),'processed_elsewhere_count':len(all_done-ids),
        'batch_processed_count':len(all_done),'batch_pending_count':len(pending),'batch_excluded_count':len(skipped)}
    value['excluded_sources']=[item for item in outcomes(data).values() if item['state']=='excluded']
    value['extraction']={'reviews':copy.deepcopy(selected)}
    value['segment_findings']=parts or []
    return value


def summary_context(data,parts):
    """只携带分段所引观点及必要事实，不嵌套完整报告；参数：聚合、分段稿；返回：输入。"""
    keys={(r['review_id'],r['observation_id']) for part in parts for f in part['findings'] for a in f['facts'] for r in a['observation_refs']}
    selected=[]
    for row in rows(data):
        value=copy.deepcopy(row);value['observations']=[o for o in row['observations'] if (row['id'],o['observation_id']) in keys]
        if value['observations']:selected.append(value)
    value=context(data,selected,parts)
    value['statistics']=process.statistics(rows(data))['statistics']
    value['basis_coverage']={'selected_observations':len(keys),'all_observations':sum(len(r['observations']) for r in rows(data))}
    value['projection_format']='总结依据分段发现及其所引观点、原文和全量程序统计；未引用观点不代表不存在。分段仅携带必要事实，完整稿另外保存。'
    return value


report_assessment=v4.report_assessment


def content(data,narrative=None,diagnostics=None,report_input=None,phase_reports=None):
    """保存新任务报告及可读阶段成果；参数：聚合、稿、诊断、输入、阶段；返回：JSON。"""
    value=v4.content(data,narrative,diagnostics,report_input);value['schema_version']=REPORT_VERSION
    states=outcomes(data);counts={s:0 for s in ('valid','partial','no_information','excluded','unfinished')}
    for source in data['sources']:counts[states.get(source['review_id'],{}).get('state','unfinished')]+=1
    value['source_outcomes']=[states.get(s['review_id'],{'review_id':s['review_id'],'state':'unfinished','reason':'执行尚未形成结果，不能当无价值评价。'}) for s in data['sources']]
    value['coverage'].update(state_counts=counts,excluded_count=counts['excluded'],unprocessed_count=counts['unfinished'],
        complete=counts['unfinished']==0,handled_count=data['task']['source_count']-counts['unfinished'])
    value['program_issues']=[i for i in value['program_issues'] if i['review_id'] not in excluded_ids(data)]
    value['phase_reports']=copy.deepcopy(phase_reports or [])
    has_facts=any(o['eligibility']['basic_fact'] for row in rows(data) for o in row['observations'])
    value['report_state']='available' if narrative else 'stage_only' if value['phase_reports'] else 'insufficient_information' if counts['unfinished']==0 and not has_facts else 'not_generated'
    for phase in value['phase_reports']:
        for item in v4.review_items(phase['compiled']):
            item['key']='segment:'+str(phase['segment_id'])+':'+item['key']
            if item['card_id']:item['card_id']='segment:'+str(phase['segment_id'])+':'+item['card_id']
            value['review_items'].append(item)
    return value
