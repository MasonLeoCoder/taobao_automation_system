"""问答报告的只读阅读模型：目录、明确标签及原文折叠，不改持久报告。"""
import html
import re
from uuid import uuid4
from ecommerce.modules.question_analysis import report,checks
from ecommerce.modules.review_analysis.v6_evidence import Blocks


def outside_positions(text,characters):
    """查找引号和括号外的标记；参数：文字及候选字符；返回：位置或不完整时None。"""
    pairs={'“':'”','‘':'’','"':'"',"'":"'",'（':'）','(':')','[':']','【':'】','「':'」','『':'』'}
    stack=[];positions=[]
    for index,char in enumerate(text):
        if stack and char==stack[-1]:stack.pop();continue
        if stack and stack[-1] in ('”','’','"',"'",'」','』'):continue
        if char in pairs:stack.append(pairs[char]);continue
        if char in ('）',')',']','】','」','』','”','’'):return None
        if not stack and char in characters:positions.append(index)
    return None if stack else positions


def paragraph_layout(rendered):
    """只整理明确顶层编号/区分和冗余提示；参数：安全HTML；返回：只读排版。"""
    parser=Blocks(rendered);parser.feed(rendered);parser.close();blocks=parser.blocks
    for index in range(len(blocks)-1,-1,-1):
        block=blocks[index]
        if block['tag']!='p':continue
        original=rendered[block['start']:block['end']];replacement=original
        # 仅合并紧邻程序折叠框的独立提示；混有分析内容或相隔其他块则保留。
        if block['text'].strip() in ('依据原文：','依据原文:','原文依据：','原文依据:') and index+1<len(blocks):
            following=blocks[index+1]
            if not rendered[block['end']:following['start']].strip() and rendered[following['start']:].startswith('<details class="ui-report-evidence">'):
                replacement=''
        match=re.fullmatch(r'<p>([^<>]*)</p>',original,re.S)
        if replacement and match:
            value=html.unescape(match[1]);positions=outside_positions(value,'①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳')
            if positions and len(positions)>1:
                markers=[value[pos] for pos in positions];prefix=value[:positions[0]]
                sequential=markers==list('①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'[:len(markers)])
                separated=all(value[:pos].rstrip().endswith(('；',';','。','！','？')) or value[pos-1]=='\n' for pos in positions[1:])
                if sequential and separated and (not prefix.strip() or (len(prefix)<60 and prefix.rstrip().endswith(('：',':')))):
                    rows=[]
                    for n,start in enumerate(positions):
                        end=positions[n+1] if n+1<len(positions) else len(value)
                        rows.append('<li><span class="ui-report-key-label">'+value[start]+'</span><span>'+html.escape(value[start+1:end])+'</span></li>')
                    replacement='<div class="qa-numbered-block">'+('<p><strong>'+html.escape(prefix)+'</strong></p>' if prefix else '')+'<ol class="ui-report-numbered-list qa-numbered-list">'+''.join(rows)+'</ol></div>'
            elif value.startswith(('区分：','区分:')):
                separators=outside_positions(value[3:],'；')
                if separators:
                    body=value[3:];ends=[pos+1 for pos in separators]+[len(body)];start=0;rows=[]
                    for end in ends:
                        if body[start:end]:rows.append('<p>'+html.escape(body[start:end])+'</p>')
                        start=end
                    replacement='<div class="qa-explanation"><p><strong>'+html.escape(value[:3])+'</strong></p>'+''.join(rows)+'</div>'
            elif value.startswith('成本因素'):
                replacement='<p><strong>成本因素</strong>'+html.escape(value[4:])+'</p>'
        rendered=rendered[:block['start']]+replacement+rendered[block['end']:]
    return rendered


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
    rendered=paragraph_layout(evidence_html(content));matches=[]
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
