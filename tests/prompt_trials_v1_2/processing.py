"""V1.2独立试验的局部校验、程序状态与统计；不读写数据库，不联网。"""
import copy
from collections import Counter
import hashlib
import json
import re
from tests.prompt_trials_v1_2 import contracts as c


def decode(text, finish_reason='stop'):
    """严格解析完整响应；参数：文字、结束原因；返回：对象及错误，不修复原文。"""
    if finish_reason!='stop':return None,[{'path':'$','reason':'响应未完整结束'}]
    try:value=json.loads(text)
    except (ValueError,TypeError) as error:
        return None,[{'path':'$','reason':'严格JSON解析失败','offset':getattr(error,'pos',None)}]
    return (value,[]) if isinstance(value,dict) else (None,[{'path':'$','reason':'根节点不是对象'}])


def extraction(value, sources):
    """逐项保留可追溯提取，原值不改；参数：响应对象及来源；返回：派生记录及完整诊断。"""
    issues=[]; audit=[]; exclusions=[]; projected=[]
    structure=c.errors(value,c.EXTRACTION)
    lookup={row['id']:row for row in sources}
    if not isinstance(value,dict) or value.get('schema_version')!='reviews-extraction-v1.2' or not isinstance(value.get('reviews'),list):
        return {'reviews':[],'structure_issues':structure,'issues':[{'path':'$','reason':'顶层契约不符'}],'adjustments':[],'exclusions':[]}

    def canonical(uid):
        """只接受源身份或唯一逐字十进制类型恢复；参数：编号；返回：原编号或None。"""
        if type(uid) is int and uid in lookup:return uid
        if isinstance(uid,str):
            matches=[key for key in lookup if str(key)==uid]
            if len(matches)==1:return matches[0]
        return None

    counts=Counter(canonical(row.get('id')) for row in value['reviews'] if isinstance(row,dict))
    def issue(uid,path,reason):
        """记录局部问题；参数：编号、路径、原因；返回：无。"""
        issues.append({'review_id':uid,'path':path,'reason':reason})

    for index,raw in enumerate(value['reviews']):
        if not isinstance(raw,dict):issue(None,f'reviews[{index}]','评价不是对象');continue
        uid=canonical(raw.get('id'))
        if uid is None or counts[uid]!=1:
            issue(uid,f'reviews[{index}].id','额外、无效或重复身份，未选择其中一条');continue
        start=len(issues)
        if type(raw['id']) is not int:audit.append({'review_id':uid,'path':'id','before':raw['id'],'after':uid,'reason':'唯一逐字匹配，仅恢复来源类型；原返回不变'})
        source=lookup[uid]
        segments={'initial':source['text'],**{a['segment']:a['text'] for a in source['appends']}}
        evidences=[]; allowed={}
        raw_evidence=raw.get('evidence',[])
        if not isinstance(raw_evidence,list):raw_evidence=[];issue(uid,'evidence','不是数组')
        ec=Counter(e.get('evidence_id') for e in raw_evidence if isinstance(e,dict) and isinstance(e.get('evidence_id'),str))
        for index,e in enumerate(raw_evidence):
            reason=c.errors(e,c.EVIDENCE)
            if not reason and (ec[e['evidence_id']]!=1 or not e['quote'].strip() or e['quote'] not in segments.get(e['segment'],'')):
                reason=['引文非精确连续原文、为空或编号重复']
            if reason:issue(uid,f'evidence[{index}]','；'.join(reason));exclusions.append({'review_id':uid,'kind':'evidence','raw':e});continue
            allowed[e['evidence_id']]=e;evidences.append(copy.deepcopy(e))
        allowed_ids=set(allowed)
        observations=[]
        raw_observations=raw.get('observations',[])
        if not isinstance(raw_observations,list):raw_observations=[];issue(uid,'observations','不是数组')
        oc=Counter(o.get('observation_id') for o in raw_observations if isinstance(o,dict) and isinstance(o.get('observation_id'),str))
        for index,point in enumerate(raw_observations):
            reason=c.errors(point,c.OBSERVATION)
            if not reason:
                refs=set(point['evidence_ids'])
                if oc[point['observation_id']]!=1 or not refs<=allowed_ids:reason.append('观点编号重复或原文引用无效')
                for u in point['uncertainties']:
                    if not set(u['evidence_ids'])<=refs:reason.append('疑点引用不属于本观点')
                if point['comparison'] and not set(point['comparison']['evidence_ids'])<=refs:reason.append('比较引用不属于本观点')
            if reason:issue(uid,f'observations[{index}]','；'.join(reason));exclusions.append({'review_id':uid,'kind':'observation','raw':point});continue
            clean=copy.deepcopy(point)
            if point['explicit_impact'] is not None and (not point['explicit_impact'].strip() or not any(point['explicit_impact'] in allowed[e]['quote'] for e in point['evidence_ids'])):
                issue(uid,f'observations[{index}].explicit_impact','影响文字不能在本观点依据定位，仅该字段在派生副本置空')
                clean['explicit_impact']=None
                audit.append({'review_id':uid,'path':f'observations[{index}].explicit_impact','before':point['explicit_impact'],'after':None,'reason':'只接受原文明示影响；原返回保持不变'})
            uncertain={field for u in point['uncertainties'] for field in u['fields']}
            clean['eligibility']={'basic_fact':not bool(uncertain&{'fact_text','topic','information_types','sentiment'}),
                'current_purchase':point['event_scope']=='当前购买' and 'event_scope' not in uncertain,
                'uncertain_fields':sorted(uncertain),'semantic_review':'未人工复核；机器只核对结构及关联，不证明分类含义正确'}
            observations.append(clean)
        usage=[]
        raw_usage=raw.get('usage_stages',[])
        if not isinstance(raw_usage,list):raw_usage=[];issue(uid,'usage_stages','不是数组')
        for index,stage in enumerate(raw_usage):
            reason=c.errors(stage,c.USAGE)
            if not reason and (not set(stage['evidence_ids'])<=allowed_ids or any(allowed[e]['segment']!=stage['segment'] for e in stage['evidence_ids'])):reason.append('阶段依据无效或跨片段')
            if reason:issue(uid,f'usage_stages[{index}]','；'.join(reason));continue
            clean=copy.deepcopy(stage)
            duration=stage['duration_text']
            if duration is not None and (not duration.strip() or not any(duration in allowed[e]['quote'] for e in stage['evidence_ids'])):
                issue(uid,f'usage_stages[{index}].duration_text','时间次数无法在本片段依据定位，派生副本置空')
                clean['duration_text']=None
                audit.append({'review_id':uid,'path':f'usage_stages[{index}].duration_text','before':duration,'after':None,'reason':'原文不支持该时间次数；其余阶段保留，原返回不变'})
            usage.append(clean)
        relations=[]
        valid_oids={o['observation_id'] for o in observations}
        raw_relations=raw.get('relations',[])
        if not isinstance(raw_relations,list):raw_relations=[];issue(uid,'relations','不是数组')
        for index,relation in enumerate(raw_relations):
            reason=c.errors(relation,c.RELATION)
            if not reason and (not set(relation['observation_ids'])<=valid_oids or not set(relation['evidence_ids'])<=allowed_ids):reason.append('关系引用已隔离或不存在')
            if reason:issue(uid,f'relations[{index}]','；'.join(reason));continue
            relations.append(copy.deepcopy(relation))
        flags=[]
        raw_flags=raw.get('flags',[])
        if not isinstance(raw_flags,list):raw_flags=[];issue(uid,'flags','不是数组')
        for index,flag in enumerate(raw_flags):
            reason=c.errors(flag,c.FLAG)
            if not reason and not set(flag['evidence_ids'])<=allowed_ids:reason.append('标记引用无效')
            if reason:issue(uid,f'flags[{index}]','；'.join(reason));continue
            flags.append(copy.deepcopy(flag))
        blank=all(not text.strip() for text in segments.values())
        if blank and (observations or evidences or usage):
            issue(uid,'$','空白原文却返回信息，派生记录隔离全部提取')
            exclusions.append({'review_id':uid,'kind':'blank_extraction','raw':raw})
            observations=[];evidences=[];usage=[];relations=[];flags=[]
        usable=[o for o in observations if o['eligibility']['basic_fact']]
        nongeneral=[o for o in usable if set(o['information_types'])-{'笼统表态'}]
        if blank:status='empty'
        elif nongeneral or usage:status='informative'
        elif usable:status='general_only'
        elif any(f['code']=='局部难以理解' for f in flags):status='unclear'
        else:status='pending'
        row_issues=issues[start:]
        projected.append({'id':uid,'information_status':status,'processing_status':'pending' if status=='pending' else 'usable_with_issues' if row_issues else 'usable',
            'evidence':evidences,'usage_stages':usage,'observations':observations,'relations':relations,'flags':flags,'local_issues':copy.deepcopy(row_issues)})
    present={r['id'] for r in projected}
    for uid in lookup:
        if uid not in present:issue(uid,'id','缺少可唯一对应的评价结果')
    return {'reviews':projected,'structure_issues':structure,'issues':issues,'adjustments':audit,'exclusions':exclusions}


def statistics(rows):
    """按来源范围和记录编号去重，局部对象未知不丢整点；参数：派生记录；返回：统计和排除明细。"""
    groups={};excluded=[]
    for row in rows:
        for point in row['observations']:
            if not point['eligibility']['basic_fact']:
                excluded.append({'review_id':row['id'],'observation_id':point['observation_id'],'reason':'核心事实/主题/类型/倾向有疑点','fields':point['eligibility']['uncertain_fields']});continue
            event=point['event_scope'] if 'event_scope' not in point['eligibility']['uncertain_fields'] else '不明确'
            entries=[('主题',point['topic'])]+[('信息类型',kind) for kind in point['information_types']]
            for kind,label in entries:
                key=(kind,label,event)
                group=groups.setdefault(key,{'review_ids':set(),'refs':{},'sentiments':{},'source_types':set(),'uncertain_fields':set()})
                group['review_ids'].add(row['id']);group['refs'][(row['id'],point['observation_id'])]=True
                group['sentiments'].setdefault(point['sentiment'],set()).add(row['id'])
                group['source_types'].add(point['source_type']);group['uncertain_fields'].update(point['eligibility']['uncertain_fields'])
    result=[]
    for (kind,label,event),group in sorted(groups.items()):
        uid='s-'+hashlib.sha256(json.dumps([kind,label,event],ensure_ascii=False).encode()).hexdigest()[:12]
        result.append({'id':uid,'kind':kind,'label':label,'event_scope':event,'count':len(group['review_ids']),
            'review_ids':sorted(group['review_ids']),'observation_refs':[{'review_id':r,'observation_id':o} for r,o in sorted(group['refs'])],
            'sentiments':{k:{'count':len(v),'review_ids':sorted(v)} for k,v in sorted(group['sentiments'].items())},
            'source_types':sorted(group['source_types']),'uncertain_fields':sorted(group['uncertain_fields']),
            'counting_unit':'评价记录，非买家；同条同组一次，跨组不可相加；同义名称未合并'})
    return {'statistics':result,'excluded_observations':excluded}


def union_count(stats, ids):
    """仅对明确选定的统计来源取编号并集；参数：统计和编号；返回：合并数量与来源，禁止混时间范围。"""
    lookup={s['id']:s for s in stats}
    if not ids or not set(ids)<=set(lookup):raise ValueError('合并来源不存在或为空')
    chosen=[lookup[key] for key in set(ids)]
    if len({s['event_scope'] for s in chosen})!=1:raise ValueError('不能混合历史、当前或不明归属')
    reviews=sorted({uid for s in chosen for uid in s['review_ids']})
    return {'source_statistic_ids':sorted(set(ids)),'event_scope':chosen[0]['event_scope'],'count':len(reviews),'review_ids':reviews,'semantic_review':'分组含义需复核；程序仅计算明确来源并集'}


def report(value, incoming):
    """核对逐项报告关联，保留有效发现；参数：返回和程序输入；返回：派生报告及诊断。"""
    structure=c.errors(value,c.REPORT)
    issues=[];warnings=[];findings=[];cards=[]
    def issue(path,reason):
        """记录局部错误；参数：路径及原因；返回：无。"""
        issues.append({'path':path,'reason':reason})
    if not isinstance(value,dict) or value.get('schema_version')!='competitor-report-v1.2':
        return {'structure_issues':structure,'issues':[{'path':'$','reason':'报告顶层版本不符'}],'warnings':[],
                'compiled':{'scope':incoming['scope'],'findings':[],'opportunities':[],'decision_summary':[],'business_checks':[],'next_actions':[],'data_issues':[]}}
    rows=incoming['extraction']['reviews']
    obs={(r['id'],o['observation_id']):(r,o) for r in rows for o in r['observations']}
    evidence={(r['id'],e['evidence_id']):e for r in rows for e in r['evidence']}
    stats={s['id']:s for s in incoming['statistics']}
    raw_findings=value.get('findings',[])
    if not isinstance(raw_findings,list):raw_findings=[]
    counts=Counter(f.get('id') for f in raw_findings if isinstance(f,dict) and isinstance(f.get('id'),str))

    def refs_valid(refs):
        """检查观点在本报告有效输入中存在；参数：引用数组；返回：布尔值。"""
        return all((r['review_id'],r['observation_id']) in obs for r in refs)

    for index,f in enumerate(raw_findings):
        path=f'findings[{index}]'
        header=copy.deepcopy(c.FINDING)
        header['properties']['facts']=c.array({'type':'object'},minimum=1)
        header['properties']['interpretation']={}
        header['properties']['statistic_ids']=c.array({})
        if c.errors(f,header):issue(path,'发现主体结构不合格');continue
        if counts[f['id']]!=1:issue(path,'发现编号重复');continue
        clean=copy.deepcopy(f);valid_facts=[];ref_set=set()
        fact_counts=Counter(fact.get('fact_id') for fact in f['facts'] if isinstance(fact.get('fact_id'),str))
        for n,fact in enumerate(f['facts']):
            if c.errors(fact,c.FACT):
                issue(path+f'.facts[{n}]','事实子结构不合格，仅隔离该事实');continue
            if fact_counts[fact['fact_id']]!=1:
                issue(path+f'.facts[{n}]','同一发现中事实编号重复，未选择其中之一');continue
            refs={(r['review_id'],r['observation_id']) for r in fact['observation_refs']}
            expected={(uid,eid) for uid,oid in refs if (uid,oid) in obs for eid in obs[(uid,oid)][1]['evidence_ids']}
            quoted={(e['review_id'],e['evidence_id']) for e in fact['evidence_refs']}
            if not refs_valid(fact['observation_refs']) or not quoted<=set(evidence) or not quoted<=expected or any(not obs[k][1]['eligibility']['basic_fact'] for k in refs):
                issue(path+f'.facts[{n}]','事实引用不存在、与观点不匹配或核心事实有疑点');continue
            if not {uid for uid,_ in refs}<={uid for uid,_ in quoted}:
                issue(path+f'.facts[{n}]','观点缺少对应评价的原文引用');continue
            valid_facts.append(copy.deepcopy(fact));ref_set.update(refs)
        if not valid_facts:issue(path,'无可追溯事实，发现不进入派生报告');continue
        clean['facts']=valid_facts
        interpretation=f['interpretation']
        if interpretation:
            if c.errors(interpretation,c.INTERPRETATION) or not refs_valid(interpretation['basis_observation_refs']):
                issue(path+'.interpretation','推断引用不存在');clean['interpretation']=None
            else:ref_set.update((r['review_id'],r['observation_id']) for r in interpretation['basis_observation_refs'])
        valid_stats=[];seen_stats=set()
        for n,selected in enumerate(f['statistic_ids']):
            if c.errors(selected,c.TEXT) or selected in seen_stats:
                issue(path+f'.statistic_ids[{n}]','统计编号无效或重复，仅隔离该引用');continue
            seen_stats.add(selected)
            stat=stats.get(selected)
            statrefs={(r['review_id'],r['observation_id']) for r in stat['observation_refs']} if stat else set()
            if not stat or not statrefs&ref_set:
                issue(path+f'.statistic_ids[{n}]','统计不存在或与本发现观点没有关联');continue
            valid_stats.append(selected)
        clean['statistic_ids']=valid_stats
        clean['semantic_review']='待内容复核；引用关联检查不证明结论含义成立'
        findings.append(clean)
    fids={f['id'] for f in findings}
    fact_lookup={(f['id'],fact['fact_id']):fact for f in findings for fact in f['facts']}
    raw_cards=value.get('opportunities',[])
    if not isinstance(raw_cards,list):raw_cards=[]
    counts=Counter(x.get('id') for x in raw_cards if isinstance(x,dict) and isinstance(x.get('id'),str))
    for index,card in enumerate(raw_cards):
        path=f'opportunities[{index}]'
        header=copy.deepcopy(c.OPPORTUNITY);header['properties']['counter_evidence']=c.array({'type':'object'})
        if c.errors(card,header):issue(path,'方向主体结构不合格');continue
        if index>=3 or counts[card['id']]!=1 or not set(card['supporting_finding_ids'])<=fids:
            issue(path,'方向数量、身份或支持反证关联不合格');continue
        clean=copy.deepcopy(card);clean['counter_evidence']=[]
        for n,counter in enumerate(card['counter_evidence']):
            if c.errors(counter,c.COUNTER):
                issue(path+f'.counter_evidence[{n}]','反证子结构不合格，仅隔离该反证');continue
            fact=fact_lookup.get((counter['finding_id'],counter['fact_id']))
            if fact is None:
                issue(path+f'.counter_evidence[{n}]','反证未能定位到可追溯具体事实，仅隔离该反证');continue
            clean['counter_evidence'].append({**copy.deepcopy(counter),'inherited_observation_refs':copy.deepcopy(fact['observation_refs']),
                'inherited_evidence_refs':copy.deepcopy(fact['evidence_refs']),'support_review':'引用存在与关联通过；解释是否削弱方向仍待语义复核'})
        cards.append(clean)
    oids={x['id'] for x in cards}
    summary=[]
    for index,item in enumerate(value.get('decision_summary',[]) if isinstance(value.get('decision_summary'),list) else []):
        if index>=5 or c.errors(item,c.SUMMARY) or not set(item['finding_ids'])<=fids or not set(item['opportunity_ids'])<=oids:
            issue(f'decision_summary[{index}]','摘要结构或关联不合格');continue
        summary.append(copy.deepcopy(item))
    checks=[];materials={m['id'] for m in incoming.get('materials',[])}
    for index,item in enumerate(value.get('business_checks',[]) if isinstance(value.get('business_checks'),list) else []):
        if c.errors(item,c.BUSINESS) or not set(item['material_refs'])<=materials or (item['status']=='已提供' and not item['material_refs']):
            issue(f'business_checks[{index}]','经营资料结构或来源不合格');continue
        checks.append(copy.deepcopy(item))
    actions=[];orders=set()
    for index,item in enumerate(value.get('next_actions',[]) if isinstance(value.get('next_actions'),list) else []):
        if index>=3 or c.errors(item,c.ACTION) or item['order'] in orders or not set(item['related_finding_ids'])<=fids or not set(item['related_opportunity_ids'])<=oids:
            issue(f'next_actions[{index}]','行动结构、顺序或引用不合格');continue
        actions.append(copy.deepcopy(item));orders.add(item['order'])
    compiled={'schema_version':'compiled-competitor-report-v1.2','scope':copy.deepcopy(incoming['scope']),
        'decision_summary':summary,'findings':findings,'opportunities':cards,'business_checks':checks,'next_actions':actions,
        'data_issues':[copy.deepcopy(x) for x in value.get('data_issues',[]) if not c.errors(x,c.REPORT['properties']['data_issues']['items'])] if isinstance(value.get('data_issues'),list) else [],
        'program_statistics':copy.deepcopy(incoming['statistics']),'review_status':'unreviewed'}
    def number_warnings(item,path='$'):
        """定位正文可能复写的统计数量；参数：模型内容和路径；返回：无，不自动否定语义。"""
        if isinstance(item,dict):
            for key,sub in item.items():number_warnings(sub,path+'.'+key)
        elif isinstance(item,list):
            for index,sub in enumerate(item):number_warnings(sub,f'{path}[{index}]')
        elif isinstance(item,str) and re.search(r'(?:\d+|[零一二两三四五六七八九十百]+)\s*条(?:评价|反馈|记录)?|\d+\s*[%％]',item):
            warnings.append({'path':path,'text':item,'reason':'可能复写统计数量，需区分原文、覆盖信息、统计和行动建议；仅警示，不自动驳回报告'})
    number_warnings(value)
    return {'structure_issues':structure,'issues':issues,'warnings':warnings,'compiled':compiled}
