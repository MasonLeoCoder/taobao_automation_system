"""可追溯的本地报告检查：格式兼容不改写引文，生成与引用状态独立。"""
import re

VERSION='reviews-local-check-v2'
SECTIONS=('总体判断','使用场景与需求','产品优点','产品缺点','用户痛点','改进与选品方向','采购与拿样清单')
MARKER=re.compile(r'【([^【】\n]{1,160})】')
METADATA=re.compile(r'(?:评价\s*)?(?P<id>[1-9][0-9]{0,18})\s*[｜|]\s*(?P<label>初评|追评\s*(?P<n>[1-9][0-9]{0,8}))')


def metadata(body):
    """仅去除明确包裹编号/标签的排版；参数：标记正文；返回：编号身份或None，不补猜字段。"""
    value=body.strip()
    for _ in range(3):
        before=value
        value=re.sub(r'(\*\*|__|`|\*)(评价|初评|追评\s*[1-9][0-9]{0,8})\1',r'\2',value)
        value=re.sub(r'(?<![0-9])(\*\*|__|`|\*)([1-9][0-9]{0,18})\1(?![0-9])',r'\2',value)
        for wrap in ('**','__','`','*'):
            if value.startswith(wrap) and value.endswith(wrap) and METADATA.fullmatch(value[len(wrap):-len(wrap)].strip()):value=value[len(wrap):-len(wrap)].strip()
        if value==before:break
    match=METADATA.fullmatch(value)
    if match is None:return None
    return {'review_id':int(match['id']),'segment':'initial' if match['label']=='初评' else 'append:'+str(int(match['n']))}


def generation(text,finish_reason,group=False):
    """只判定正文生成是否完整；参数：原文、结束原因、是否组稿；返回：独立状态，不考虑引用。"""
    if not isinstance(text,str) or not text.strip():
        return {'state':'empty','complete':False,'finish_reason':finish_reason,'missing_sections':[],'issues':['没有返回可读正文。']}
    headings=re.findall(r'^\s*#{1,3}\s+(.+?)\s*$',text,re.M)
    missing=[] if group else [name for name in SECTIONS if not any(name in h for h in headings)]
    issues=(['接口返回未完整结束，已保留可读部分，未自动续写。'] if finish_reason!='stop' else [])+['缺少必要章节：'+name for name in missing]
    state='interrupted' if finish_reason!='stop' else 'incomplete_sections' if missing else 'available'
    return {'state':state,'complete':state=='available','finish_reason':finish_reason,'missing_sections':missing,'issues':issues}


def quoted(text,start):
    """读取标记之后的原文引号内容，保留每个字；参数：整稿、标记末位置；返回：引文或None及末位置。"""
    # 只兼容标签周围的排版和换行，不能跨过另一引用或正文段落寻找引号。
    prefix=re.match(r'[ \t]*(?:(?:\*\*|__|`|\*)[ \t]*)*(?:\n[ \t]*)?(?:(?:原文)(?:\*\*|__|`|\*)*[ \t]*[:：][ \t]*(?:(?:\*\*|__|`|\*)[ \t]*)*(?:\n[ \t]*)?)?([“"])',text[start:])
    if prefix is None:return None,start
    left=start+prefix.end();opener=prefix[1];closer='”' if opener=='“' else '"';depth=1
    for index in range(left,min(len(text),left+65536)):
        char=text[index]
        if char=='\n' and text[index:index+2]=='\n\n':break
        if char==opener and opener!=closer:depth+=1
        elif char==closer:
            depth-=1
            if depth==0:return text[left:index],index+1
    return None,start


def check(text,finish_reason,sources,group=False):
    """引用次数、原文匹配及生成分别检查；参数：正文和固定来源；返回：不含语义通过承诺的结果。"""
    generated=generation(text,finish_reason,group);text=text if isinstance(text,str) else ''
    lookup={s['id']:s for s in sources};citations=[];issues=[];consumed=0
    for match in MARKER.finditer(text or ''):
        if match.start()<consumed:continue
        body=match[1];identity=metadata(body)
        if identity is None and not (re.search(r'评价|初评|追评',body) or re.match(r'\s*[0-9]',body)):continue
        quote,end=quoted(text,match.end());consumed=end
        source=lookup.get(identity['review_id']) if identity else None;original=None
        if source:
            original=source['text'] if identity['segment']=='initial' else next((a['text'] for a in source.get('appends',[]) if a['segment']==identity['segment']),None)
        recognized=identity is not None and quote is not None
        valid=recognized and original is not None and bool(quote.strip()) and quote in original
        if identity is None:reason='引用编号或初评/追评标签不完整，未猜测归属。'
        elif source is None:reason='该评价编号不在本批固定来源中。'
        elif original is None:reason='该初评或追评序号不在对应来源中。'
        elif quote is None:reason='未识别到带完整引号的原文引用，请对照原稿核对。'
        elif not quote.strip():reason='引用文字为空，无法核对依据。'
        elif not valid:reason='引文包含省略号且未与原文连续对应，请核对是否省略或拼接。' if ('…' in quote or '...' in quote) else '引文未与原文逐字连续对应，未改写或自动放行。'
        else:reason='连续原文逐字对应；结论含义仍需人工核对。'
        entry={**(identity or {'review_id':None,'segment':None}),'index':len(citations)+1,'marker':match[0],
            'quote':quote,'valid':bool(valid),'recognized':recognized,'reason':reason,'original_text':original if not valid else None,
            'start':match.start(),'end':end if quote is not None else match.end()}
        citations.append(entry)
        if not valid:issues.append('第'+str(entry['index'])+'处引用：'+reason)
    if not citations:issues.append('没有识别到引用标记，依据待人工核对；正文生成状态单独展示。')
    if re.search(r'(?:满意率|缺陷率|退货率|复购率)\s*(?:为|达到|约)?\s*\d+(?:\.\d+)?\s*[%％]',text or ''):
        issues.append('正文出现未经本地核实的比例，请人工核对，不作为程序统计。')
    valid_count=sum(c['valid'] for c in citations);unrecognized=sum(not c['recognized'] for c in citations)
    counts={'citation_count':len(citations),'valid_count':valid_count,'pending_count':len(citations)-valid_count,
        'unrecognized_count':unrecognized,'invalid_count':len(citations)-valid_count-unrecognized,
        'valid_source_count':len({c['review_id'] for c in citations if c['valid']})}
    return {'rule_version':VERSION,'generation':generated,'state':'complete' if not issues else 'needs_review',
        'complete':not issues,'issues':list(dict.fromkeys(issues)),'citations':citations,'counts':counts}
