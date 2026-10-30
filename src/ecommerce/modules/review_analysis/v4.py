"""新版评价提取、固定发送计划、程序统计与报告上下文；不负责网络或存储。"""
import copy
import json
import math
from pathlib import Path
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.modules.review_analysis import v4_contracts as contract,v4_processing as process

VERSION='reviews-v4'
POLICY='reviews-validation-v4.1'
REPORT_VERSION='reviews-report-v4'
MAX_INPUT=60000
EXTRACT_OUTPUT=12288
REPORT_OUTPUT=16384
MAX_TOKENS=5000000


def encode(value):
    """紧凑编码；参数：JSON对象；返回：文字。"""
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),default=str)


def snapshot():
    """读取冻结提示及结构；参数：无；返回：任务规则快照。"""
    base=Path(__file__).parent/'prompts'
    return {'schema_version':4,'version':VERSION,'validation_policy':POLICY,
            'classify':(base/'extract_v4.txt').read_text(encoding='utf-8'),
            'report':(base/'report_v4.txt').read_text(encoding='utf-8'),
            'extraction_schema':contract.EXTRACTION,'report_schema':contract.REPORT}


def prompt(rules,kind,incoming):
    """组合实际请求；参数：冻结规则、用途、资料；返回：完整文字。"""
    return rules['classify' if kind=='classify' else 'report']+'\n完整机器输出结构：\n'+encode(rules['extraction_schema' if kind=='classify' else 'report_schema'])+'\n以下是输入资料：\n'+encode(incoming)


def reserve(text,output):
    """按UTF8字节及输出上限预留；参数：文字、输出；返回：保守Token。"""
    return len(text.encode('utf-8'))+output+256


def plan(items,rules,context,timeout):
    """固定完整来源分组及可选分段报告额度；参数：评价、规则、背景、等待；返回：计划。"""
    groups=[];batch=[]
    def estimate(rows):
        """估算原子观点输出；参数：整组来源；返回：输出预留。"""
        size=sum(len((r['text']+''.join(a['text'] for a in r['appends'])).encode()) for r in rows)
        return 2048+len(rows)*768+size*3
    def pack(rows,index):
        """固定一组完整请求；参数：来源及序号；返回：分组。"""
        text=prompt(rules,'classify',{'product_context':context,'reviews':rows})
        output=min(EXTRACT_OUTPUT,max(4096,math.ceil(estimate(rows)/1024)*1024))
        return {'index':index,'source_ids':[r['id'] for r in rows],'prompt':text,'max_output_tokens':output,
                'reserved_tokens':reserve(text,output)}
    for item in items:
        candidate=batch+[item]
        if batch and (estimate(candidate)>EXTRACT_OUTPUT or len(pack(candidate,1)['prompt'])>MAX_INPUT):
            groups.append(pack(batch,len(groups)+1));batch=[item]
        else:batch=candidate
        if estimate(batch)>EXTRACT_OUTPUT or len(pack(batch,1)['prompt'])>MAX_INPUT:
            raise ValueError('单条评价及追评超过当前完整处理边界，未截断或发送；请先核对发送计划。')
    if batch:groups.append(pack(batch,len(groups)+1))
    # 能完整放入时直接总报告；否则最多每个提取组一份分段报告，再整理总报告。
    report_slots=len(groups)+1
    report_each=MAX_INPUT*4+REPORT_OUTPUT+256
    held=report_slots*report_each
    minimum=sum(g['reserved_tokens'] for g in groups)+held
    recommended=minimum+max(4096,math.ceil(minimum/10))
    if recommended>MAX_TOKENS or len(groups)+report_slots>200:
        raise ValueError('完整处理计划超出单任务用量或次数边界，未抽样或发送；请先调整明确范围。')
    return {'version':4,'context':context,'groups':groups,'timeout_seconds':timeout,'json_mode':True,
        'report_reserved_tokens':held,'report_each_reserved_tokens':report_each,'report_output_tokens':REPORT_OUTPUT,
        'report_input_characters':MAX_INPUT,'report_slots':report_slots,'planned_calls':len(groups)+report_slots,
        'minimum_tokens':minimum,'recommended_tokens':recommended,
        'report_strategy':'可完整纳入时直接总报告；长内容按原分组生成分段报告再整理，最多预留每组一次及总报告一次；不自动重试。'}


def assess(text,sources,finish_reason='stop'):
    """逐项校验并转换持久结果；参数：原返回、来源、结束状态；返回：审计及有效条目。"""
    value,errors=process.decode(text,finish_reason)
    result=process.extraction(value,sources) if value is not None else {'reviews':[],'structure_issues':errors,'issues':[],'adjustments':[]}
    issues=[{'review_id':i.get('review_id'),'field':i.get('path','$'),'code':'v4_local','message':i.get('reason','局部结构不符')} for i in result['issues']]
    issues += [{'review_id':None,'field':'$','code':'v4_structure','message':str(i)} for i in result['structure_issues']]
    valid=[{'schema_version':'reviews-detail-v4',**r} for r in result['reviews'] if r['processing_status']!='pending']
    return {'policy':POLICY,'source_ids':[s['id'] for s in sources],'results':valid,'issues':issues,
            'adjustments':result['adjustments'],'projection':result}


def effective(detail):
    """获取最新人工副本，不修改原结果；参数：明细；返回：有效提取。"""
    return copy.deepcopy(detail['corrections'][-1]['after'] if detail['corrections'] else detail['result'])


def rows(data):
    """依来源顺序获取有效提取；参数：数据库聚合；返回：数组。"""
    by_id={d['source_id']:effective(d) for d in data['details']}
    return [by_id[s['id']] for s in sorted(data['sources'],key=lambda s:s['position']) if s['id'] in by_id and not by_id[s['id']].get('excluded',False)]


def compact(value):
    """仅省略空字段，保留全部非空内容及false/0；参数：JSON；返回：紧凑副本。"""
    if isinstance(value,dict):return {k:compact(v) for k,v in value.items() if v is not None and v!=[] and v!={}}
    if isinstance(value,list):return [compact(v) for v in value]
    return value


def context(data,selected_rows=None,parts=None):
    """生成报告输入与覆盖说明；参数：持久数据、可选组、分段发现；返回：资料。"""
    selected=rows(data) if selected_rows is None else selected_rows
    identifiers={r['id'] for r in selected};task=data['task']
    pending=[s['review_id'] for s in data['sources'] if s['review_id'] not in identifiers]
    stats=process.statistics(selected)
    scope={'product_id':data['product']['platform_product_id'],'batch_ids':list(dict.fromkeys(s['batch_id'] for s in data['sources'])),
        'sample_count':task['source_count'],'analyzed_count':len(selected),'pending_count':len(pending),
        'report_stage':'完整报告' if not pending else '阶段/分段报告',
        'limitations':['仅本批已采集评价，不代表平台全部评价、市场需求或独立买家。','主题未自动合并；买家自述、结构引用及程序计数均不等于实物或语义验证。']}
    return {'scope':scope,'pending_issues':[{'review_id':uid,'simulation':False,'issue':'本输入暂未纳入，不得补写原文或视为没有问题。'} for uid in pending],
        'product_context':task['parameters']['plan']['context'],
        'projection_format':'仅省略null/空集合；提取原结构另行保留。分段报告仅覆盖所列来源，完整覆盖和来源由程序填充。',
        'extraction':{'reviews':[compact(r) for r in selected]},'statistics':stats['statistics'],
        'statistic_exclusions':stats['excluded_observations'],'materials':[],
        'segment_findings':parts or []}


def report_assessment(response,incoming):
    """解析报告并局部保留；参数：响应、实际输入；返回：处理副本。"""
    value,errors=process.decode(response['text'],response['finish_reason'])
    if value is None:return {'structure_issues':errors,'issues':[],'warnings':[],'compiled':None}
    return process.report(value,incoming)


def summary_context(data,parts):
    """以分段发现和全量程序统计整理总报告；参数：数据、分段报告；返回：输入及依据覆盖。"""
    all_rows=rows(data);keys={(r['review_id'],r['observation_id']) for p in parts for f in p['findings'] for fact in f['facts'] for r in fact['observation_refs']}
    selected=[]
    for row in all_rows:
        row=copy.deepcopy(row);row['observations']=[o for o in row['observations'] if (row['id'],o['observation_id']) in keys]
        if row['observations']:selected.append(row)
    incoming=context(data,selected,parts)
    global_context=context(data)
    incoming['scope']=global_context['scope'];incoming['pending_issues']=global_context['pending_issues']
    incoming['statistics']=global_context['statistics']
    incoming['projection_format']='总报告依据所有已保存的分段发现、所引观点与原文及全量程序统计；不逐一复述全部观点。完整提取可独立查看，未引用观点不代表不存在。'
    incoming['basis_coverage']={'selected_observations':len(keys),'all_observations':sum(len(r['observations']) for r in all_rows)}
    return incoming


def review_items(narrative):
    """生成待审核身份与候选内容；参数：有效报告；返回：条目数组。"""
    items=[]
    def add(key,kind,candidate,card=None):
        """添加待复核项；参数：身份、类型、内容、卡片；返回：无。"""
        items.append({'key':key,'kind':kind,'candidate':copy.deepcopy(candidate),'candidate_hash':fingerprint(candidate),
                      'card_id':card,'decision':'pending','revision':0})
    if not narrative:return items
    for f in narrative['findings']:
        if f['interpretation']:add('finding:'+f['id'],'分析推断',f['interpretation'])
    for card in narrative['opportunities']:
        add('opportunity:'+card['id'],'选品方向',card,card['id'])
        for i,counter in enumerate(card['counter_evidence']):add('counter:'+card['id']+':'+str(i),'反证',counter,card['id'])
    return items


def program_issues(data,current_rows):
    """由结构化来源生成问题说明，拒绝AI自由归因；参数：数据、有效提取；返回：说明数组。"""
    issues=[];present={r['id'] for r in current_rows}
    for s in data['sources']:
        if s['review_id'] not in present:issues.append({'review_id':s['review_id'],'description':f"评价{s['review_id']}尚未形成纳入统计的提取；不能推断其没有问题。"})
    for r in current_rows:
        for flag in r['flags']:issues.append({'review_id':r['id'],'description':f"评价{r['id']}：{flag['code']}，仅限制有关内容。"})
        for o in r['observations']:
            if o['uncertainties']:issues.append({'review_id':r['id'],'description':f"评价{r['id']} / {o['observation_id']}有字段疑点，原文与疑点保留，不能把线索当已验证性能。"})
    return issues


def content(data,narrative=None,diagnostics=None,report_input=None):
    """构建正式版本报告，不调用AI；参数：数据、有效AI稿、诊断及输入；返回：持久JSON。"""
    current=rows(data);statistics=process.statistics(current)['statistics']
    lookup={(r['id'],o['observation_id']):o for r in current for o in r['observations']}
    for stat in statistics:
        stat['source_counts']={s:len({ref['review_id'] for ref in stat['observation_refs'] if lookup[(ref['review_id'],ref['observation_id'])]['source_type']==s}) for s in stat['source_types']}
    revisions={d['id']:d['revision'] for d in data['details']}
    source_hash=fingerprint({'sources':[(s['id'],s['content_hash']) for s in data['sources']],'details':revisions})
    return {'schema_version':REPORT_VERSION,'statistics':statistics,'narrative':narrative,'diagnostics':diagnostics or [],
        'extraction_snapshot':copy.deepcopy(current),
        'detail_revisions':revisions,'source_hash':source_hash,'review_items':review_items(narrative),
        'program_issues':program_issues(data,current),'coverage':{'source_count':data['task']['source_count'],
            'processed_count':len(data['details']),'included_count':len(current),'unprocessed_count':data['task']['source_count']-len(data['details']),
            'report_complete':narrative is not None,'complete':len(data['details'])==data['task']['source_count']},
        'report_basis':(report_input or {}).get('basis_coverage'),
        'review_status':'unreviewed','findings':[],'suggestions':[]}
