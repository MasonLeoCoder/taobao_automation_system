"""直接报告的本地完整性/引文检查与安全展示；不能代替语义及实物验证。"""
from collections import Counter
import html
from html.parser import HTMLParser
import re
import markdown
from ecommerce.modules.question_analysis import planner as v6
from ecommerce.modules.question_analysis import checks as v6_checks
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now

SECTIONS=v6_checks.SECTIONS


def check(text,finish_reason,sources,group=False):
    """核对可读性和连续引用，不重写原返回；参数：文字、结束原因、实际来源、用途；返回：检查。"""
    return v6_checks.check(text,finish_reason,sources,group)


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
        self.parts.append('<'+tag+(' href="'+href+'"' if tag=='a' and re.fullmatch(r'#question-[1-9]\d*',href) else '')+'>')
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
    escaped=html.escape(text if isinstance(text,str) else '')
    escaped=v6_checks.MARKER.sub(lambda m:'['+m[0]+'](#question-'+str(v6_checks.metadata(m[1])['question_id'])+')' if v6_checks.metadata(m[1]) else m[0],escaped)
    parser=SafeHTML();parser.feed(markdown.markdown(escaped,extensions=['tables','fenced_code']));parser.close()
    return ''.join(parser.parts)


def content(data,final=None,phases=None,issues=None,local=False,origin='execution'):
    """保存源范围与可读阶段成果，不伪造逐条分类数量；参数：聚合、总稿、分组、说明；返回：JSON。"""
    sources=[s['snapshot'] for s in data['sources']];sent=set()
    for call in data['calls']:
        sent.update(call['request']['source_ids'])
    skus=Counter(s.get('sku') or '未记录规格' for s in sources)
    validation=check(final['text'],final['finish_reason'],sources) if final else check('',None,sources)
    generated=validation.pop('generation')
    if final is None:generated.update(state='not_generated',issues=[])
    value={'schema_version':v6.REPORT_VERSION,'markdown':final['text'] if final and isinstance(final.get('text'),str) else '',
        'validation':validation,'generation':generated,
        'phases':phases or [],'issues':issues or [],'origin':'local_empty' if local else 'ai',
        'source_hash':fingerprint([(s['id'],s['content_hash']) for s in data['sources']]),'detail_revisions':{},
        'coverage':{'source_count':len(sources),'submitted_count':len(sent),'referenced_count':validation['counts']['valid_source_count'],
            'scope_note':'来源数、提交数和已引用数是不同口径；不是逐条语义核验通过数或独立买家数。'},
        'basic_statistics':{'sku_counts':dict(skus),'answer_count':sum(len(s.get('answers',[])) for s in sources)},
        'review_status':'unreviewed'}
    if local:
        value['validation'].update(state='not_applicable',issues=[],citations=[],complete=True)
        value['generation'].update(state='insufficient_information',complete=True,missing_sections=[],issues=[])
    value['inspection']={'rule_version':v6_checks.VERSION,'checked_at':database_now().isoformat()+'+08:00',
        'origin':origin,'response_hash':fingerprint(final) if final else None,'source_hash':value['source_hash'],
        'based_on_report_id':data['report']['id'] if origin=='local_recheck' and data.get('report') else None}
    return value


def message(report):
    """用生成状态和引用数量生成准确说明；参数：报告JSON；返回：安全显示文字。"""
    generated=report['generation'];counts=report['validation']['counts']
    if generated['state']=='insufficient_information':return '本批无可分析文字，已保存资料不足说明，未调用AI。'
    if not generated['complete']:return '报告生成未完成，已保存可读内容和原返回；'+('；'.join(generated['issues']) or '尚无完整总报告。')
    if not counts['citation_count']:return '报告已生成；未识别到可核对的引用标记，依据待人工核对。'
    return f'报告已生成；{counts["valid_count"]}处引用通过，{counts["pending_count"]}处待核对。结论含义仍需人工核对。'
