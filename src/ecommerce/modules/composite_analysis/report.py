"""综合报告的本地检查与安全阅读；文字匹配不能代替语义或实物验证。"""
import html
import re
from uuid import uuid4
from ecommerce.modules.review_analysis.v6_checks import MARKER, quoted, metadata as review_metadata
from ecommerce.modules.question_analysis.checks import metadata as question_metadata
from ecommerce.modules.question_analysis.report import render as safe_render
from ecommerce.modules.review_analysis.v6_evidence import Blocks
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now

VERSION = 'composite-report-v1'
CHECK_VERSION = 'composite-local-check-v1'
SECTIONS = ('总体判断', '使用场景与核心需求', '评价与问大家的交叉发现', '产品优势与采购底线',
            '缺点、痛点与风险', '选品与调整方向', '拿样验证清单', '供应商确认与信息缺口', '下一步行动')


def generation(text, finish_reason, group=False):
    """生成完整性与依据检查分离；参数：正文、结束原因、用途；返回：生成状态。"""
    text = text if isinstance(text, str) else ''
    headings = re.findall(r'^\s*#{1,3}\s+(.+?)\s*$', text, re.M)
    missing = [] if group else [name for name in SECTIONS if not any(name in h for h in headings)]
    issues = (['接口未完整结束，未自动续写。'] if finish_reason != 'stop' else []) + ['缺少章节：'+s for s in missing]
    if not text.strip(): issues.append('尚无可读总报告。')
    return {'complete': not issues, 'state': 'available' if not issues else 'incomplete',
            'finish_reason': finish_reason, 'missing_sections': missing, 'issues': issues}


def evidence(incoming):
    """读取实际发送的去重原文；参数：固定输入；返回：类别标记映射。"""
    return {entry['marker']: entry for source in incoming['reports'] for entry in source['evidence']}


def canonical(body):
    """只识别带明确类别的引用，禁止将裸编号猜成评价或问题。"""
    if body.strip().startswith('评价'):
        value = review_metadata(body)
        if value:
            label = '初评' if value['segment'] == 'initial' else '追评'+value['segment'].split(':')[1]
            return f'【评价{value["review_id"]}｜{label}】'
    value = question_metadata(body)
    if value:
        return f'【问题{value["question_id"]}' + (f'｜回答{value["answer_id"]}' if value['answer_id'] else '') + '】'
    return None


def check(text, finish_reason, incoming, group=False):
    """核对实际发送原文中的连续引用，不接受只在数据库但未发送的依据。"""
    text = text if isinstance(text, str) else ''; lookup = evidence(incoming); citations = []; consumed = 0
    for match in MARKER.finditer(text):
        if match.start() < consumed or not re.search(r'评价|问题|回答|初评|追评|^\d', match[1]): continue
        marker = canonical(match[1]); quote, end = quoted(text, match.end()); consumed = max(end, match.end())
        source = lookup.get(marker); original = source['text'] if source else None
        valid = bool(original is not None and quote and quote.strip() and quote in original)
        citations.append({'index': len(citations)+1, 'marker': match[0], 'canonical': marker,
            'quote': quote, 'start': match.start(), 'end': end if quote is not None else match.end(),
            'valid': valid, 'original_text': original, 'source_key': source['id'] if source else None,
            'reason': '连续原文对应；结论含义仍需人工核对。' if valid else '类别、编号或连续原文未匹配本次发送依据，请核对。'})
    good = sum(v['valid'] for v in citations)
    return {'rule_version': CHECK_VERSION, 'generation': generation(text, finish_reason, group),
        'state': 'complete' if citations and good == len(citations) else 'needs_review', 'citations': citations,
        'counts': {'citation_count': len(citations), 'valid_count': good, 'pending_count': len(citations)-good,
                   'valid_source_count': len({v['source_key'] for v in citations if v['valid']})}}


def content(data, origin='execution', issues=None):
    """从持久返回生成版本，不改原返回或基础报告；参数：聚合；返回：报告JSON。"""
    final = next((v for v in reversed(data['calls']) if v['chunk_index'] == 0 and v.get('response')), None)
    response = final['response'] if final else {}; text = response.get('text') or ''
    incoming = data['task']['parameters']['plan']['input']; validation = check(text, response.get('finish_reason'), incoming)
    generated = validation.pop('generation')
    if not final: generated['state'] = 'not_generated'
    return {'schema_version': VERSION, 'markdown': text, 'generation': generated, 'validation': validation,
        'phases': [{'index': v['chunk_index'], 'text': v['response']['text']} for v in data['calls'] if v['chunk_index'] > 0 and v.get('response')],
        'source_reports': [{k: v for k, v in s.items() if k not in ('markdown', 'evidence')} for s in incoming['reports']],
        'source_hash': fingerprint(incoming), 'detail_revisions': {}, 'issues': issues or [],
        'inspection': {'rule_version': CHECK_VERSION, 'origin': origin, 'checked_at': database_now().isoformat()+'+08:00'},
        'review_status': 'unreviewed'}


def message(value):
    """准确区分生成与引用状态；参数：报告；返回：进度。"""
    if not value['generation']['complete']:
        return '综合报告尚未完整生成；已保存内容保留，未自动重试。' + '；'.join(value['issues'] or value['generation']['issues'])
    counts = value['validation']['counts']
    return f'报告已生成；{counts["valid_count"]}/{counts["citation_count"]}处引用匹配，{counts["pending_count"]}处待核对。' + ('未识别到引用，依据待核对。' if not counts['citation_count'] else '') + '结论含义仍需人工判断。'


def render(value):
    """安全渲染、连续依据默认折叠且逐条成行；参数：持久报告；返回：HTML。"""
    text = value['markdown']; groups = []
    for entry in value['validation']['citations']:
        if entry['quote'] is None: continue
        if groups and not text[groups[-1][-1]['end']:entry['start']].strip(' \t\r\n。；;'): groups[-1].append(entry)
        else: groups.append([entry])
    prefix = 'COMPOSITEEVIDENCE'+uuid4().hex; replacements = []
    for group in reversed(groups):
        token = prefix+str(group[0]['index']); rows = []
        for entry in group:
            key = entry['index']; label = html.escape(entry['marker']); quote = html.escape(entry['quote'])
            rows.append(f'<div class="ui-report-evidence-item"><a href="#composite-citation-{key}">{label}</a> 原文：“{quote}”</div>')
        value_html = f'<details class="ui-report-evidence"><summary>查看原文依据 · {len(group)}处引用</summary><div class="ui-report-evidence-body">'+''.join(rows)+'</div></details>'
        replacements.append((token, value_html)); text = text[:group[0]['start']]+token+text[group[-1]['end']:]
    rendered = safe_render(text)
    for token, markup in replacements: rendered = rendered.replace('<p>'+token+'</p>', markup).replace(token, markup)
    return rendered


def reading(value, renderer=render):
    """拆分顶层章节用于目录与重点卡；参数：报告；返回：只读阅读模型。"""
    rendered = renderer(value); parser = Blocks(rendered); parser.feed(rendered); parser.close()
    starts = {b['start'] for b in parser.blocks if b['tag'] in ('h1', 'h2', 'h3')}
    matches = [m for m in re.finditer(r'<h([123])>(.*?)</h\1>', rendered, re.S) if m.start() in starts]
    sections = []
    for i, match in enumerate(matches):
        sections.append({'id': 'composite-section-'+str(i), 'heading': match[2],
            'html': rendered[match.end():matches[i+1].start() if i+1 < len(matches) else len(rendered)], 'lead': i == 0})
    return {'intro': rendered[:matches[0].start()] if matches else rendered, 'sections': sections}
