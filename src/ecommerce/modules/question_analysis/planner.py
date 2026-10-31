"""直接报告的资料投影、容量规划及固定发送计划；不联网、不改原始资料。"""
import copy
import json
import math
from pathlib import Path
from ecommerce.infrastructure.ai.capabilities import capability,Counter,cost_ceiling
from ecommerce.infrastructure.storage.task_store import fingerprint

VERSION='questions-v1'
REPORT_VERSION='questions-report-v1'
MAX_TASK_TOKENS=5000000
TRANSPORT_BYTES=16*1024*1024


def encode(value):
    """紧凑编码，原文不改写；参数：对象；返回：文字。"""
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),default=str)


def snapshot():
    """冻结主报告与分组要求；参数：无；返回：新规则。"""
    base=Path(__file__).parent/'prompts'
    return {'schema_version':1,'version':VERSION,'report':(base/'report.txt').read_text(encoding='utf-8'),
            'group':(base/'group.txt').read_text(encoding='utf-8'),'source_format':'questions-source-v1','output_format':'markdown'}


def project(item):
    """投影已脱敏的问答快照；参数：问题；返回：独立副本。"""
    return copy.deepcopy(item)


def messages(rules,title,items,kind='direct',parts=None):
    """组织单次system/user消息；参数：规则、标题、问答、用途及分组稿；返回：消息。"""
    incoming={'product':title or '商品名称未提供'}
    if parts is None:incoming['questions']=items
    else:
        incoming['group_findings']=parts
        incoming['basis_note']='以下是各组的主题发现和必要原文引用。只按明确依据汇总，保留规格/条件差异；完整原文在系统保留，未引用不等于不存在。'
    return [{'role':'system','content':rules['group' if kind=='group' else 'report']},
            {'role':'user','content':encode(incoming)}]


def prompt_view(values):
    """生成仅供查看的文字，实际消息另存；参数：消息；返回：显示文本。"""
    return '\n\n'.join('【'+m['role']+'】\n'+m['content'] for m in values)


def capacity_ok(counter,profile,values,output):
    """检查模型与独立网络保护；参数：估算器、能力、消息、输出；返回：是否可发及估算。"""
    measurement=counter.measure(values,output)
    return measurement['reserved_tokens']<=profile['context_tokens'] and len(encode(values).encode())<=TRANSPORT_BYTES-1024,measurement


def fragments(item,rules,title,counter,profile,output):
    """优先按完整回答拆分；极长单条保留位置和问题上下文，不能截掉资料。"""
    units=[];context={k:v for k,v in item.items() if k!='answers'}
    def fits(row):
        """检查单元实际容量；参数：单元；返回：布尔。"""
        return capacity_ok(counter,profile,messages(rules,title,[row],'group'),output)[0]
    # 问题本身无法容纳时拒绝预检，避免丢失回答所需语境。
    if not fits(dict(context,answers=[])):
        raise ValueError('单个问题正文超出可用容量，请明确缩小分析范围或选择容量更大的模型；未发送或截断。')
    for answer in item.get('answers',[]):
        row=dict(context,answers=[answer])
        if fits(row):units.append(row);continue
        text=answer['text'];start=0
        while start<len(text):
            lo=start+1;hi=len(text);best=start
            def candidate(end):
                """保留回答身份与原文位置；参数：末位置；返回：问答片段。"""
                part=dict(answer,text=text[start:end],fragment={'start':start,'end':end,'source_length':len(text)})
                return dict(context,answers=[part])
            while lo<=hi:
                mid=(lo+hi)//2
                if fits(candidate(mid)):best=mid;lo=mid+1
                else:hi=mid-1
            if best==start:raise ValueError('最小回答片段也无法容纳，未发送。')
            units.append(candidate(best));start=best
            if len(units)>199:raise ValueError('单题分片超过请求保护，未发送。')
    return units or [dict(context,answers=[])]


def plan(root,config,model,items,context,rules,output_tokens=None):
    """先规划全部容量、汇总及预算；参数：配置和完整资料；返回：可审查固定计划。"""
    profile=capability(config,model);counter=Counter(root,config,profile)
    output=config.report_output_tokens if output_tokens is None else output_tokens
    if type(output) is not int or not 512<=output<=profile['max_output_tokens']:
        raise ValueError('报告输出上限须为512～当前模型最大输出量。')
    group_output=min(config.group_output_tokens,output,profile['max_output_tokens'])
    title=context.get('title');projected=[project(i) for i in items]
    whole=messages(rules,title,projected);fits,measurement=capacity_ok(counter,profile,whole,output)
    groups=[];summary=None
    def add(rows,index,kind,out):
        """冻结实际消息和来源范围；参数：资料、序号、用途、输出；返回：无。"""
        values=messages(rules,title,rows,kind);ok,size=capacity_ok(counter,profile,values,out)
        if not ok:raise ValueError('完整请求超出模型容量或16MiB网络保护，未发送。')
        groups.append({'index':index,'kind':kind,'source_ids':list(dict.fromkeys(r['id'] for r in rows)),
            'questions':rows,'messages':values,'prompt':prompt_view(values),'max_output_tokens':out,
            'measurement':size,'reserved_tokens':size['reserved_tokens'],'request_hash':fingerprint(values)})
    no_text=not any(i['text'].strip() or any(a['text'].strip() for a in i.get('answers',[])) for i in items)
    if not no_text and fits:add(projected,0,'direct',output)
    elif not no_text:
        units=[]
        for item in projected:
            units.extend([item] if capacity_ok(counter,profile,messages(rules,title,[item],'group'),group_output)[0]
                         else fragments(item,rules,title,counter,profile,group_output))
        packed=[]
        for unit in units:
            if packed and not capacity_ok(counter,profile,messages(rules,title,packed+[unit],'group'),group_output)[0]:
                add(packed,len(groups)+1,'group',group_output);packed=[]
            packed.append(unit)
        if packed:add(packed,len(groups)+1,'group',group_output)
        placeholders=[{'index':g['index'],'question_ids':g['source_ids'],'findings':''} for g in groups]
        base=counter.count(messages(rules,title,[],'summary',placeholders))
        # 使用同族本地分词数据仍按两倍输出预留；缺少数据则按保守字节估算放大，不能假称精确。
        generated=sum(g['max_output_tokens'] for g in groups)*(2 if counter.tool else 1024)
        bound=base+generated;margin=max(2048,math.ceil(bound*0.2))
        summary={'index':0,'kind':'summary','max_output_tokens':output,'input_bound':bound,
            'reserved_tokens':bound+output+margin,'safety_tokens':margin}
        if summary['reserved_tokens']>profile['context_tokens']:
            raise ValueError('分组后的汇总预留也超过模型容量；未发出任何请求，不自动增加汇总层级。请核对分词资料或调整明确范围。')
    calls=len(groups)+int(summary is not None);minimum=sum(g['reserved_tokens'] for g in groups)+(summary['reserved_tokens'] if summary else 0)
    if calls>200 or minimum>MAX_TASK_TOKENS:raise ValueError('计划超过单任务200次或500万Token用量保护；未发送，不自动扩大额度。')
    recommended=min(MAX_TASK_TOKENS,minimum+max(256,math.ceil(minimum/20))) if calls else 0
    input_bound=sum(g['measurement']['input_tokens']+g['measurement']['safety_tokens'] for g in groups)+(summary['input_bound']+summary['safety_tokens'] if summary else 0)
    return {'version':6,'mode':'local_empty' if no_text else 'direct' if summary is None else 'grouped',
        'context':copy.deepcopy(context),'capability':profile,'counter':counter.identity,'groups':groups,'summary':summary,
        'report_output_tokens':output,'group_output_tokens':group_output,'timeout_seconds':config.analysis_timeout_seconds,
        'planned_calls':calls,'minimum_tokens':minimum,'recommended_tokens':recommended,'whole_measurement':measurement,
        'estimated_cost':cost_ceiling(profile,input_bound,sum(g['max_output_tokens'] for g in groups)+(output if summary else 0)),
        'source_count':len(items),'source_ids':[i['id'] for i in items],
        'report_strategy':'容量足够时一次直接生成七部分报告；超限才分组归纳加一次汇总；计划和费用确认后不自动加请求。'}


def summary_messages(task,calls):
    """只合并已完成的精简分组稿；参数：任务、已核对调用；返回：最终消息。"""
    parts=[{'index':c['chunk_index'],'question_ids':c['request']['source_ids'],'findings':c['response']['text']} for c in calls]
    return messages(task['rules'],task['parameters']['plan']['context'].get('title'),[],'summary',parts)
