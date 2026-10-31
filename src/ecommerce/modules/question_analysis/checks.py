"""问答报告本地检查；引用匹配不代表结论语义或性能经过验证。"""
import re
from ecommerce.modules.review_analysis.v6_checks import quoted

VERSION='questions-local-check-v1'
SECTIONS=('总体判断','使用场景与需求','购买顾虑与决策因素','回答中的优缺点与用户痛点','未解决问题与矛盾反馈','选品与改进方向','采购与拿样验证清单')
MARKER=re.compile(r'【([^【】\n]{1,160})】')
METADATA=re.compile(r'问题\s*([1-9]\d{0,18})(?:\s*[｜|]\s*回答\s*([1-9]\d{0,18}))?')


def metadata(body):
    """读取明确问题及回答编号；参数：标记内部；返回：归属或None，不猜编号。"""
    match=METADATA.fullmatch(body.strip())
    return {'question_id':int(match[1]),'answer_id':int(match[2]) if match[2] else None} if match else None


def generation(text,finish_reason,group=False):
    """单独检查章节和结束原因；参数：正文、接口结束原因、分组标记；返回：生成状态。"""
    text=text if isinstance(text,str) else ''
    headings=re.findall(r'^\s*#{1,3}\s+(.+?)\s*$',text,re.M)
    missing=[] if group else [s for s in SECTIONS if not any(s in h for h in headings)]
    issues=(['接口未完整结束，未自动续写。'] if finish_reason!='stop' else [])+['缺少章节：'+s for s in missing]
    if not text.strip():issues.append('尚无可读总报告。')
    return {'complete':not issues,'state':'available' if not issues else 'incomplete','issues':issues,'missing_sections':missing,'finish_reason':finish_reason}


def check(text,finish_reason,sources,group=False):
    """逐处核对问题回答归属及连续原文；参数：报告和冻结来源；返回：引用检查。"""
    text=text if isinstance(text,str) else '';lookup={s['id']:s for s in sources};citations=[];consumed=0
    for match in MARKER.finditer(text):
        if match.start()<consumed or '问题' not in match[1]:continue
        identity=metadata(match[1]);quote,end=quoted(text,match.end());consumed=end
        question=lookup.get(identity['question_id']) if identity else None;original=None
        if question:
            original=question['text'] if identity['answer_id'] is None else next((a['text'] for a in question['answers'] if a['id']==identity['answer_id']),None)
        valid=original is not None and bool(quote and quote.strip()) and quote in original
        reason='连续原文对应；结论含义仍需人工判断。' if valid else '编号、所属问题或连续原文未匹配，请对照原文核对。'
        citations.append({**(identity or {'question_id':None,'answer_id':None}),'index':len(citations)+1,'marker':match[0],
            'quote':quote,'start':match.start(),'end':end,'valid':bool(valid),'recognized':identity is not None and quote is not None,
            'reason':reason,'original_text':original if not valid else None})
    good=sum(c['valid'] for c in citations);pending=len(citations)-good
    issues=[] if citations and not pending else ['存在待核对引用。' if pending else '未识别到引用，依据待人工核对。']
    return {'rule_version':VERSION,'generation':generation(text,finish_reason,group),'state':'complete' if not issues else 'needs_review',
        'complete':not issues,'issues':issues,'citations':citations,'counts':{'citation_count':len(citations),'valid_count':good,'pending_count':pending,
        'valid_source_count':len({c['question_id'] for c in citations if c['valid']})}}
