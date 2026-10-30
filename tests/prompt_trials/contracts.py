"""V1.1完整输出契约及固定子集检查；无第三方依赖，不调用AI。"""
import json
import re


def string(*, enum=None, nullable=False, pattern=None):
    """构造文字约束；参数：枚举、空值及模式；返回：结构定义。"""
    value={'type':['string','null'] if nullable else 'string'}
    if enum is not None:value['enum']=list(enum)+([None] if nullable else [])
    if pattern is not None:value['pattern']=pattern
    if not nullable:value['minLength']=1
    return value


def array(item, *, minimum=0, maximum=None, unique=False):
    """构造数组约束；参数：元素和数量限制；返回：结构定义。"""
    value={'type':'array','items':item,'minItems':minimum}
    if maximum is not None:value['maxItems']=maximum
    if unique:value['uniqueItems']=True
    return value


def obj(**properties):
    """构造固定字段对象；参数：全部字段；返回：禁止额外键的结构。"""
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}


def nullable(value):
    """构造可空对象；参数：非空结构；返回：可空结构。"""
    return {'anyOf':[value,{'type':'null'}]}


TEXT=string()
NULL_TEXT=string(nullable=True)
ID={'type':'integer','minimum':1}
SEGMENT=string(pattern=r'^(initial|append:[1-9][0-9]*)$')
EID=string(pattern=r'^e[1-9][0-9]*$')
OID=string(pattern=r'^o[1-9][0-9]*$')
FID=string(pattern=r'^f[1-9][0-9]*$')
OPID=string(pattern=r'^p[1-9][0-9]*$')
TYPES=('购买原因','使用场景','产品反馈','问题与不满','期望与需求','规格反馈','回购经历','回购意愿','不再购买','比较','取舍','笼统表态')
FIELDS=('information_types','topic','fact_text','sentiment','target','target_detail','source_type','event_scope','comparison','explicit_impact')
EIDS=array(EID,minimum=1,unique=True)
REF=obj(review_id=ID,observation_id=OID)
EREF=obj(review_id=ID,evidence_id=EID)
TEXTS=array(TEXT)
SCOPE=string(enum=('当前购买','历史购买','不明确'))
COMPARISON=obj(reference_object=NULL_TEXT,dimension=NULL_TEXT,direction=string(enum=('当前更高','当前更低','相当','不明确')),description=NULL_TEXT,evidence_ids=EIDS)
UNCERTAINTY=obj(fields=array(string(enum=FIELDS),minimum=1,unique=True),reason=TEXT,evidence_ids=EIDS)
OBSERVATION=obj(observation_id=OID,information_types=array(string(enum=TYPES),minimum=1,unique=True),topic=TEXT,
    fact_text=TEXT,sentiment=string(enum=('正面','负面','中性','褒贬并存','不明确')),
    target=string(enum=('产品','包装','物流','客服','不明确')),target_detail=NULL_TEXT,
    source_type=string(enum=('使用体验自述','收货或外观感受','期望','转述','不明确')),
    event_scope=SCOPE,comparison=nullable(COMPARISON),explicit_impact=NULL_TEXT,evidence_ids=EIDS,
    uncertainties=array(UNCERTAINTY))
EVIDENCE=obj(evidence_id=EID,segment=SEGMENT,quote=TEXT)
USAGE=obj(segment=SEGMENT,stage=string(enum=('未使用','已使用','不明确')),duration_text=NULL_TEXT,evidence_ids=EIDS)
RELATION=obj(relation_type=string(enum=('观点变化','相互矛盾','表现取舍')),observation_ids=array(OID,minimum=2,unique=True),description=TEXT,evidence_ids=EIDS)
FLAG=obj(code=string(enum=('局部难以理解','疑似截断','资料指令','其他')),message=TEXT,evidence_ids=array(EID,unique=True))
REVIEW=obj(id=ID,evidence=array(EVIDENCE),usage_stages=array(USAGE),observations=array(OBSERVATION),relations=array(RELATION),flags=array(FLAG))
EXTRACTION=obj(schema_version={'const':'reviews-extraction-v1.1'},reviews=array(REVIEW))

FACT=obj(text=TEXT,observation_refs=array(REF,minimum=1,unique=True),evidence_refs=array(EREF,minimum=1,unique=True))
INTERPRETATION=obj(text=TEXT,basis_observation_refs=array(REF,minimum=1,unique=True),assumptions=TEXTS,limitations=TEXTS)
STATREF=obj(stat_id=TEXT,meaning=TEXT)
FINDING=obj(id=FID,category=string(enum=('购买需求与场景','认可表现','问题与需求','规格与取舍','竞品综合')),
    facts=array(FACT,minimum=1),interpretation=nullable(INTERPRETATION),statistics=array(STATREF),
    evidence_strength=string(enum=('证据较充分','线索性发现','暂无法判断')),strength_reason=TEXT,limitations=TEXTS)
COUNTER=obj(finding_id=FID,explanation=TEXT)
OPPORTUNITY=obj(id=OPID,name=TEXT,stage=string(enum=('现成款采购','包装组合调整','后续定制储备')),
    target_need=TEXT,product_direction=TEXT,supporting_finding_ids=array(FID,minimum=1,unique=True),reasoning=TEXT,
    counter_evidence=array(COUNTER),risk_hypotheses=TEXTS,conditions=TEXTS,missing_information=TEXTS,
    validation_actions=array(TEXT,minimum=1),stop_conditions=TEXTS,
    priority=string(enum=('优先拿样验证','补充资料后判断','暂不优先')),priority_reason=TEXT,independence_reason=TEXT)
SUMMARY=obj(text=TEXT,finding_ids=array(FID,minimum=1,unique=True),opportunity_ids=array(OPID,unique=True),limitation=NULL_TEXT)
BUSINESS=obj(item=TEXT,status=string(enum=('已提供','未提供','存在疑点')),known_information=NULL_TEXT,
    material_refs=array(TEXT,unique=True),missing_information=TEXT,decision_impact=TEXT)
ACTION=obj(order={'type':'integer','minimum':1,'maximum':3},action=TEXT,purpose=TEXT,
    related_finding_ids=array(FID,minimum=1,unique=True),related_opportunity_ids=array(OPID,unique=True),
    continue_condition=NULL_TEXT,stop_condition=NULL_TEXT)
REPORT=obj(schema_version={'const':'competitor-report-v1.1'},decision_summary=array(SUMMARY,maximum=5),
    findings=array(FINDING),opportunities=array(OPPORTUNITY,maximum=3),business_checks=array(BUSINESS),
    next_actions=array(ACTION,maximum=3),data_issues=array(obj(description=TEXT,affected_sections=TEXTS,handling=TEXT)))


def errors(value, schema, path='$'):
    """检查本模块定义用到的固定约束子集；参数：值、结构及位置；返回：全部错误。"""
    if 'anyOf' in schema:
        return [] if any(not errors(value,variant,path) for variant in schema['anyOf']) else [path+': 不符合可空结构']
    issues=[]
    if 'const' in schema and value!=schema['const']:issues.append(path+': 固定值不符')
    expected=schema.get('type')
    expected=[expected] if isinstance(expected,str) else expected
    types={'null':value is None,'string':isinstance(value,str),'integer':type(value) is int,
           'array':isinstance(value,list),'object':isinstance(value,dict)}
    if expected is not None and not any(types.get(kind,False) for kind in expected):
        return issues+[path+': 类型不符']
    if 'enum' in schema and value not in schema['enum']:issues.append(path+': 枚举越界')
    if isinstance(value,str):
        if len(value)<schema.get('minLength',0):issues.append(path+': 文字为空')
        if 'pattern' in schema and not re.search(schema['pattern'],value):issues.append(path+': 编号或片段格式不符')
    if type(value) is int:
        if value<schema.get('minimum',value) or value>schema.get('maximum',value):issues.append(path+': 数值越界')
    if isinstance(value,dict):
        properties=schema.get('properties',{})
        issues += [path+'.'+key+': 缺失' for key in schema.get('required',[]) if key not in value]
        if schema.get('additionalProperties') is False:issues += [path+'.'+key+': 额外字段' for key in value if key not in properties]
        for key,sub in properties.items():
            if key in value:issues.extend(errors(value[key],sub,path+'.'+key))
    if isinstance(value,list):
        if len(value)<schema.get('minItems',0) or len(value)>schema.get('maxItems',len(value)):issues.append(path+': 数组数量越界')
        if schema.get('uniqueItems') and len({json.dumps(x,sort_keys=True,ensure_ascii=False) for x in value})!=len(value):issues.append(path+': 数组重复')
        for index,item in enumerate(value):issues.extend(errors(item,schema.get('items',{}),f'{path}[{index}]'))
    return issues


def documents():
    """返回用于发送及归档的完整机器契约；参数：无；返回：两个结构对象。"""
    return {'extraction':EXTRACTION,'report':REPORT}
