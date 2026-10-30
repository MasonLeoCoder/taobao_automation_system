"""程序来源、统计和待复核建议；只处理内存副本，不推断自然语言正确性。"""
import copy
import hashlib
import json
from collections import defaultdict

SENTIMENTS=('正面','负面','中性','褒贬并存','不明确')
DECISIONS=('待复核','已采用','不采用')
ACTION_TYPES=('筛选现成款','调整组合','改善包装','后续改款')
STOP_SCOPES=('当前款','供应商','包装组合','产品方向')
FIELD_LABELS={'information_types':'信息类型','topic':'主题','fact_text':'观点含义','sentiment':'评价倾向',
              'target':'评价对象','target_detail':'具体对象或部件','source_type':'表述来源','event_scope':'当前或历史归属',
              'comparison':'比较信息','explicit_impact':'明确影响'}


def digest(value):
    """计算内容身份；参数：JSON对象；返回：SHA256。"""
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def ref_key(ref):
    """转为严格观点身份；参数：引用；返回：二元组，无效时抛错。"""
    if set(ref)!={'review_id','observation_id'} or type(ref['review_id']) is not int or not isinstance(ref['observation_id'],str):
        raise ValueError('观点引用类型或字段不符')
    return ref['review_id'],ref['observation_id']


def source_views(rows):
    """保留全部表达和原疑点，不提升为已验证性能；参数：已校验提取；返回：观点视图。"""
    views=[]
    for row in rows:
        evidence={e['evidence_id']:e for e in row['evidence']}
        for point in row['observations']:
            if not point['evidence_ids'] or not set(point['evidence_ids'])<=set(evidence):
                raise ValueError('表达缺少有效依据')
            views.append({'review_id':row['id'],**copy.deepcopy(point),
                'evidence':[copy.deepcopy(evidence[eid]) for eid in point['evidence_ids']],
                'usage_stages':copy.deepcopy(row.get('usage_stages',[])),
                'expression_status':'有疑点表达线索' if not point['eligibility']['basic_fact'] else '可追溯的买家表达',
                'performance_status':'未独立验证；自述、外观、期望和转述均不能代替实测',
                'review_status':'待复核'})
    return views


def statistics(views,selected=None):
    """按原主题/类型/时间归属统计并分倾向和来源；参数：观点、可选统计ID；返回：计数视图。"""
    groups={}
    for v in views:
        if not v['eligibility']['basic_fact']:continue
        event='不明确' if 'event_scope' in v['eligibility']['uncertain_fields'] else v['event_scope']
        for kind,label in [('主题',v['topic'])]+[('信息类型',t) for t in v['information_types']]:
            key=(kind,label,event)
            g=groups.setdefault(key,{'review_ids':set(),'sentiments':defaultdict(set),'sources':defaultdict(set),'refs':set()})
            g['review_ids'].add(v['review_id']);g['sentiments'][v['sentiment']].add(v['review_id'])
            g['sources'][v['source_type']].add(v['review_id']);g['refs'].add((v['review_id'],v['observation_id']))
    result=[]
    for (kind,label,event),g in sorted(groups.items()):
        # 与冻结试验相同的编号公式；只在原范围内解释，不是跨报告全局身份。
        sid='s-'+hashlib.sha256(json.dumps([kind,label,event],ensure_ascii=False).encode()).hexdigest()[:12]
        if selected is not None and sid not in selected:continue
        result.append({'id':sid,'kind':kind,'label':label,'event_scope':event,'count':len(g['review_ids']),
            'review_ids':sorted(g['review_ids']),
            'observation_refs':[{'review_id':r,'observation_id':o} for r,o in sorted(g['refs'])],
            'sentiments':{s:{'count':len(g['sentiments'][s]),'review_ids':sorted(g['sentiments'][s])} for s in SENTIMENTS},
            'sources':{s:{'count':len(ids),'review_ids':sorted(ids)} for s,ids in sorted(g['sources'].items())},
            'counting_note':'提及数不是认可数。各倾向/来源分别按评价记录去重，同条可同时出现不同倾向，不得相加；不是买家数或市场比例。'})
    return result


def program_issues(incoming,rows,result):
    """从结构化身份生成资料说明，忽略模型自由编号；参数：输入、提取、诊断；返回：问题数组。"""
    issues=[]
    for p in incoming['pending_issues']:
        issues.append({'kind':'待处理','review_id':p['review_id'],'observation_id':None,
                       'description':f"评价{p['review_id']}："+('模拟暂不纳入，不能据此认为没有问题。' if p.get('simulation') else '未形成可用提取，原因不能从缺失推断。')})
    for row in rows:
        for flag in row['flags']:
            issues.append({'kind':'原提取标记','review_id':row['id'],'observation_id':None,
                           'description':f"评价{row['id']}：{flag['code']}。标记仍待内容复核，不扩大为整条无效。"})
        for p in row['observations']:
            fields=p['eligibility']['uncertain_fields']
            if fields:issues.append({'kind':'字段疑点','review_id':row['id'],'observation_id':p['observation_id'],
                'description':f"评价{row['id']} / {p['observation_id']}：{ '、'.join(FIELD_LABELS.get(f,f) for f in fields)}存在疑点；只限制相应内容。"})
    for issue in result['issues']:
        issues.append({'kind':'原返回局部隔离','review_id':None,'observation_id':None,
                       'description':issue['path']+'：'+issue['reason']+'。原返回未改写。'})
    return issues


def validate_proposal(proposal,lookup):
    """检查助手建议的身份与动作结构，不证明语义；参数：建议、观点索引；返回：无。"""
    required={'name','reason','basis','checks'}
    if set(proposal)!=required or not all(isinstance(proposal[k],str) and proposal[k].strip() for k in ('name','reason')):
        raise ValueError('建议字段不完整')
    if not proposal['basis'] or not {ref_key(r) for r in proposal['basis']}<=set(lookup):raise ValueError('建议引用超出本阶段')
    if not proposal['checks']:raise ValueError('建议缺少验证动作')
    for check in proposal['checks']:
        if set(check)!={'problem','action_type','action','continue_condition','stop_scope','stop_condition'}:raise ValueError('验证动作字段不完整')
        if check['action_type'] not in ACTION_TYPES or check['stop_scope'] not in STOP_SCOPES:raise ValueError('动作或淘汰范围无效')
        if not all(isinstance(v,str) and v.strip() for v in check.values()):raise ValueError('验证动作存在空值')


def build(phase,incoming,extracted,result,assessment,binding):
    """生成不覆盖原稿的收尾报告；参数：阶段、输入、提取、原派生、助手意见、来源身份；返回：新派生对象。"""
    views=source_views(extracted);lookup={(v['review_id'],v['observation_id']):v for v in views}
    compiled=result['compiled'];groups=[];seen={};finding_alias={}
    for f in compiled['findings']:
        refs={ref_key(r) for fact in f['facts'] for r in fact['observation_refs']}
        if not refs<=set(lookup):raise ValueError('原报告发现引用超出本阶段')
        signature=tuple(sorted(refs))
        canonical=seen.setdefault(signature,f['id']);finding_alias[f['id']]=canonical
        groups.append({'id':f['id'],'canonical_id':canonical,'category':f['category'],
            'observation_refs':[{'review_id':r,'observation_id':o} for r,o in sorted(refs)],
            'statistic_ids':copy.deepcopy(f['statistic_ids'])})
    cards=[];items=[]
    expected={c['id'] for c in compiled['opportunities']}
    if set(assessment['cards'])!=expected:raise ValueError('助手建议与原卡片身份不一致')
    for card in compiled['opportunities']:
        aid=card['id'];notes=assessment['cards'][aid];proposal=notes['proposal']
        validate_proposal(proposal,lookup)
        item_id=phase+':'+aid+':proposal'
        item={'id':item_id,'kind':'方向与动作修订建议','card_id':aid,'candidate':copy.deepcopy(proposal),'review_status':'待复核'}
        item['candidate_digest']=digest(item['candidate']);items.append(item)
        counters=[]
        if len(notes['counter_reviews'])!=len(card['counter_evidence']):raise ValueError('反证复核意见缺项')
        for i,(counter,note) in enumerate(zip(card['counter_evidence'],notes['counter_reviews'])):
            if not isinstance(note,str) or not note.strip():raise ValueError('反证意见为空')
            candidate={'original':copy.deepcopy(counter),'assistant_note':note}
            cid=phase+':'+aid+':counter:'+str(i)
            items.append({'id':cid,'kind':'原AI反证','card_id':aid,'candidate':candidate,
                          'candidate_digest':digest(candidate),'review_status':'待复核'})
            counters.append({'item_id':cid,**candidate})
        cards.append({'id':aid,'original_name':card['name'],'proposal_item_id':item_id,'proposal':copy.deepcopy(proposal),
            'original_priority':card['priority'],'supporting_finding_ids':list(dict.fromkeys(finding_alias[f] for f in card['supporting_finding_ids'])),
            'counter_reviews':counters,'pending_review_ids':[p['review_id'] for p in incoming['pending_issues']],
            'review_status':'待复核','limitation':'方向、优先级和验证动作是待确认建议；没有已独立验证的性能或采购结论。'})
    return {'schema_version':'review-report-closeout-v1','phase':phase,'baseline_digest':binding,
        'scope':copy.deepcopy(incoming['scope']),'views':views,'statistics':statistics(views),
        'findings':groups,'cards':cards,'review_items':items,'program_issues':program_issues(incoming,extracted,result),
        'business_checks':copy.deepcopy(compiled['business_checks']),
        'diagnostics':{k:copy.deepcopy(result[k]) for k in ('structure_issues','issues','warnings')},
        'business_gate':{'status':'先补基本询价与规格，再决定拿样','description':'报价、起订量、基本规格、补货条件需先核对；本轮未新增经营资料，不把未提供说成供应商没有。'},
        'review_status':'待复核','ai_calls_added':0}


def apply_decisions(report,receipts):
    """仅应用绑定当前内容的明确人工记录；参数：报告、追加记录；返回：副本，不修改原输入。"""
    out=copy.deepcopy(report);items={i['id']:i for i in out['review_items']}
    for receipt in receipts:
        if receipt['phase']!=out['phase']:continue
        if receipt['baseline_digest']!=out['baseline_digest']:raise ValueError('人工记录来源已变化，不能沿用')
        item=items.get(receipt['item_id'])
        if item is None or receipt['candidate_digest']!=item['candidate_digest']:raise ValueError('待审核内容已变化，不能沿用人工结论')
        if receipt['decision'] not in DECISIONS or receipt.get('user_confirmed') is not True or not all(isinstance(receipt.get(k),str) and receipt[k].strip() for k in ('reviewer','comment')):
            raise ValueError('缺少明确人工确认、意见或有效状态')
        item['review_status']=receipt['decision'];item['decision_record']=copy.deepcopy(receipt)
    for card in out['cards']:
        own=[i for i in items.values() if i['card_id']==card['id']]
        proposal=items[card['proposal_item_id']]
        card['review_status']='不采用' if proposal['review_status']=='不采用' else '已采用' if all(i['review_status']!='待复核' for i in own) and proposal['review_status']=='已采用' else '待复核'
    out['review_status']='已完成复核' if all(i['review_status']!='待复核' for i in items.values()) else '待复核'
    return out
