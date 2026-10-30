"""直接报告的本地完整性/引文检查与安全展示；不能代替语义及实物验证。"""
from collections import Counter
import html
from html.parser import HTMLParser
import re
import markdown
from ecommerce.modules.review_analysis import v6
from ecommerce.infrastructure.storage.task_store import fingerprint

SECTIONS=('总体判断','使用场景与需求','产品优点','产品缺点','用户痛点','改进与选品方向','采购与拿样清单')
CITATION=re.compile(r'【评价(?P<id>[1-9]\d*)[｜|](?P<label>初评|追评(?P<n>[1-9]\d*))】\s*(?:原文[：:]\s*)?[“"](?P<quote>[^\n]+?)[”"]')


def check(text,finish_reason,sources,group=False):
    """核对可读性和连续引用，不重写原返回；参数：文字、结束原因、实际来源、用途；返回：检查。"""
    issues=[];citations=[];missing=[];lookup={s['id']:s for s in sources}
    if not isinstance(text,str) or not text.strip():return {'state':'empty','issues':['接口没有返回可读文字。'],'citations':[],'complete':False}
    if finish_reason!='stop':issues.append('本次返回未完整结束，不能当完整报告；已保留可读部分，未自动续写。')
    if not group:
        headings=re.findall(r'^\s*#{1,3}\s+(.+?)\s*$',text,re.M)
        for name in SECTIONS:
            if not any(name in h for h in headings):missing.append(name);issues.append('缺少必要章节：'+name)
    for match in CITATION.finditer(text):
        uid=int(match['id']);segment='initial' if match['label']=='初评' else 'append:'+match['n']
        source=lookup.get(uid);original=None
        if source:
            original=source['text'] if segment=='initial' else next((a['text'] for a in source.get('appends',[]) if a['segment']==segment),None)
        valid=original is not None and bool(match['quote'].strip()) and match['quote'] in original
        citations.append({'review_id':uid,'segment':segment,'quote':match['quote'],'valid':valid})
        if not valid:issues.append(f'评价{uid} / {segment}的引文未通过逐字对应检查，请核对原文。')
    # 未按约定格式写出的引用也必须暴露，不能把未识别当已核对。
    if not citations:issues.append('未识别到约定格式的原文引用，报告仍可阅读，关键观点需要人工核对。')
    elif len(re.findall(r'【评价\d+[｜|]',text))!=len(citations):issues.append('部分引用格式未识别，请对照原文核对。')
    if re.search(r'(?:满意率|缺陷率|退货率|复购率)\s*(?:为|达到|约)?\s*\d+(?:\.\d+)?\s*[%％]',text):issues.append('正文出现未经本地核实的比例，请人工核对，不作为程序统计。')
    state='interrupted' if finish_reason!='stop' else 'needs_review' if issues else 'complete'
    complete=finish_reason=='stop' and not missing and (group or any(c['valid'] for c in citations))
    return {'state':state,'issues':list(dict.fromkeys(issues)),'citations':citations,'complete':complete}


class SafeHTML(HTMLParser):
    """仅保留排版标签和本页来源链接，不加载模型给出的图片、脚本或外部地址。"""
    allowed={'h1','h2','h3','h4','h5','h6','p','ul','ol','li','blockquote','strong','em','code','pre','hr','br','table','thead','tbody','tr','th','td','a'}
    def __init__(self):
        """初始化；参数：无；返回：实例。"""
        super().__init__(convert_charrefs=False);self.parts=[]
    def handle_starttag(self,tag,attrs):
        """过滤标签属性；参数：标签、属性；返回：无。"""
        if tag not in self.allowed:return
        href=dict(attrs).get('href','')
        self.parts.append('<'+tag+(' href="'+href+'"' if tag=='a' and re.fullmatch(r'#review-[1-9]\d*',href) else '')+'>')
    def handle_endtag(self,tag):
        """闭合允许标签；参数：标签；返回：无。"""
        if tag in self.allowed:self.parts.append('</'+tag+'>')
    def handle_data(self,data):
        """转义显示文字；参数：文字；返回：无。"""
        self.parts.append(html.escape(data))
    def handle_entityref(self,name):
        """保留已转义实体；参数：实体；返回：无。"""
        self.parts.append('&'+name+';')
    def handle_charref(self,name):
        """保留字符实体；参数：实体；返回：无。"""
        self.parts.append('&#'+name+';')


def render(text):
    """安全渲染Markdown并链接原文；参数：模型原文；返回：过滤后的HTML。"""
    escaped=html.escape(text or '')
    escaped=re.sub(r'【评价([1-9]\d*)([｜|])',r'[【评价\1](#review-\1)\2',escaped)
    parser=SafeHTML();parser.feed(markdown.markdown(escaped,extensions=['tables','fenced_code']));parser.close()
    return ''.join(parser.parts)


def content(data,final=None,phases=None,issues=None,local=False):
    """保存源范围与可读阶段成果，不伪造逐条分类数量；参数：聚合、总稿、分组、说明；返回：JSON。"""
    sources=[s['snapshot'] for s in data['sources']];sent=set()
    for call in data['calls']:
        sent.update(call['request']['source_ids'])
    skus=Counter(s.get('sku') or '未记录规格' for s in sources)
    value={'schema_version':v6.REPORT_VERSION,'markdown':final['text'] if final else '',
        'validation':check(final['text'],final['finish_reason'],sources) if final else {'state':'not_generated','issues':[],'citations':[],'complete':False},
        'phases':phases or [],'issues':issues or [],'origin':'local_empty' if local else 'ai',
        'source_hash':fingerprint([(s['id'],s['content_hash']) for s in data['sources']]),'detail_revisions':{},
        'coverage':{'source_count':len(sources),'submitted_count':len(sent),'referenced_count':len({i['review_id'] for i in check(final['text'],final['finish_reason'],sources)['citations'] if i['valid']}) if final else 0,
            'scope_note':'来源数、提交数和已引用数是不同口径；不是逐条语义核验通过数或独立买家数。'},
        'basic_statistics':{'sku_counts':dict(skus),'with_append_count':sum(bool(s.get('appends')) for s in sources)},
        'review_status':'unreviewed'}
    if local:value['validation']={'state':'insufficient_information','issues':[],'citations':[],'complete':True}
    return value
