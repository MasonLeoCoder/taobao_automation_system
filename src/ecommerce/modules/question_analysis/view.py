"""问答报告的只读阅读模型：目录、明确标签及原文折叠，不改持久报告。"""
import html
import re
from uuid import uuid4
from ecommerce.modules.question_analysis import report,checks
from ecommerce.modules.review_analysis.v6_evidence import Blocks


def evidence_html(content):
    """合并连续引用并保留逐行定位；参数：已有报告；返回：安全HTML。"""
    text=content['markdown'];groups=[]
    for entry in content['validation']['citations']:
        if entry['quote'] is None:continue
        if groups and not text[groups[-1][-1]['end']:entry['start']].strip(' \t\r\n。；;'):
            groups[-1].append(entry)
        else:groups.append([entry])
    replacements=[];prefix='QAEVIDENCE'+uuid4().hex
    for entries in reversed(groups):
        token=prefix+str(entries[0]['index']);rows=[]
        for entry in entries:
            label=html.escape(entry['marker']);quote=html.escape(entry['quote']);index=entry['index']
            anchor='#answer-'+str(entry['answer_id']) if entry.get('answer_id') else '#question-'+str(entry['question_id']) if entry['question_id'] else '#qa-checks'
            rows.append('<div class="ui-report-evidence-item" id="qa-citation-'+str(index)+'"><a data-qa-source data-qa-return="qa-citation-'+str(index)+'" href="'+anchor+'">'+label+'</a> 原文：“'+quote+'”</div>')
        value='<details class="ui-report-evidence"><summary>查看原文依据 · '+str(len(entries))+'处引用</summary><div class="ui-report-evidence-body">'+''.join(rows)+'</div></details>'
        replacements.append((token,value));text=text[:entries[0]['start']]+token+text[entries[-1]['end']:]
    rendered=report.render(text)
    for token,value in replacements:rendered=rendered.replace('<p>'+token+'</p>',value).replace(token,value)
    return rendered


def emphasize(rendered):
    """只突出段首明示标签，不能从问句或正文猜测分类；参数：安全HTML；返回：排版。"""
    labels=('购买顾虑','使用反馈','提问者自述','待验证建议','先补资料','可进入拿样验证','暂缓当前款判断','待核实事项','未知事项','优先选品方向','优先行动')
    # 已有加粗原样保留，只装饰没有其他标签的明确段首。
    pattern=r'(<(?:p|li)>)('+ '|'.join(labels)+r')([：:])'
    rendered=re.sub(pattern,lambda m:m[1]+'<strong class="ui-report-key-label">'+m[2]+m[3]+'</strong>',rendered)
    def missing_list(match):
        """仅将明确列举的待核实项逐项换行，保留分号及文字；参数：段落；返回：列表或原段。"""
        value=match[2]
        if '；' not in value or re.search(r'[“”"‘’（）()]',value):return match[0]
        parts=re.findall(r'[^；]+；?',value)
        if len(parts)<2:return match[0]
        return '<div><p>'+match[1]+'</p><ul>'+''.join('<li>'+part+'</li>' for part in parts)+'</ul></div>'
    return re.sub(r'<p>(<strong class="ui-report-key-label">(?:待核实事项|未知事项)[：:]</strong>)([^<>]+)</p>',missing_list,rendered)


def reading(content):
    """拆分已有章节，第一章原文作为重点卡；参数：报告；返回：只读章节/导航。"""
    rendered=evidence_html(content);matches=[]
    parser=Blocks(rendered);parser.feed(rendered);parser.close()
    headings={b['start'] for b in parser.blocks if b['tag'] in ('h1','h2','h3')}
    for match in re.finditer(r'<h([123])>(.*?)</h\1>',rendered,re.S):
        if match.start() not in headings:continue
        label=html.unescape(re.sub(r'<[^>]*>','',match[2])).strip()
        normalized=re.sub(r'^(?:第?[一二三四五六七八九十0-9]+[章节、.．)）\s]+)','',label).strip()
        if normalized in checks.SECTIONS:matches.append((match,normalized))
    if not matches:return {'intro':emphasize(rendered),'sections':[]}
    sections=[]
    for index,(match,label) in enumerate(matches):
        end=matches[index+1][0].start() if index+1<len(matches) else len(rendered)
        sections.append({'id':'qa-section-'+str(index+1),'label':label,'heading':match[2],
            'html':emphasize(rendered[match.end():end]),'lead':label=='总体判断'})
    return {'intro':emphasize(rendered[:matches[0][0].start()]),'sections':sections}
