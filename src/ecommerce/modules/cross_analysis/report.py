"""交叉报告结构和来源定位检查，不把定位通过等同事实或性能验证。"""
import html
from uuid import uuid4
from ecommerce.modules.composite_analysis import report as shared
from ecommerce.modules.image_analysis.report import generation as image_generation
from ecommerce.modules.review_analysis.v6_checks import MARKER, quoted
from ecommerce.modules.question_analysis.report import render as safe_render
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now
from .kinds import REPORT_VERSION, REQUIRED, KINDS

CHECK_VERSION = 'cross-local-check-v1'


def generation(text, finish_reason, group=False):
    """只要求三项核心章节，其余动态展开，不因可选章缺失阻止阅读。"""
    return image_generation(text,finish_reason,group,sections=REQUIRED)


def check(text, incoming):
    """分别核对报告标识、图片报告图号和连续文字；不补查未发送资料。"""
    evidence = {e['marker']:dict(e,kind=r['kind'],report_id=r['report_id']) for r in incoming['reports'] for e in r['evidence']}
    reports = {r['report_marker']:r for r in incoming['reports']}; entries = []; consumed = 0
    for match in MARKER.finditer(text):
        if match.start() < consumed: continue
        marker = '【'+match[1].replace('|','｜').strip()+'】'; quote = None; end = match.end()
        reference = reports.get(marker); source = evidence.get(marker); kind = None; source_key = None
        if reference:
            valid = True; reference_type = 'report'; kind = reference['kind']; source_key = 'report:'+reference['report_id']
            reason = '标识对应所选报告；只核对归属，不证明转述含义正确。'
        elif source and source.get('reference_type') == 'image':
            valid = True; reference_type = 'image'; kind = source['kind']; source_key = source['id']
            reason = '图号对应所选图片报告已有依据；本次未重看图片，不证明宣称或含义真实。'
        else:
            canonical = shared.canonical(match[1]); source = evidence.get(canonical)
            quote,end = quoted(text,match.end()); consumed = max(end,match.end())
            valid = bool(source and quote and quote.strip() and quote in source['text'])
            reference_type = 'text'; kind = source['kind'] if source else None; source_key = source['id'] if source else None
            reason = '连续文字对应；结论含义仍需人工核对。' if valid else '未对应本次发送的报告标识、图号或连续原文，请核对。'
        entries.append(dict(index=len(entries)+1,marker=match[0],quote=quote,start=match.start(),end=end,valid=valid,
            original_text=source['text'] if source else None,reference_type=reference_type,kind=kind,source_key=source_key,reason=reason))
    good = [v for v in entries if v['valid']]; used = {v['kind'] for v in good}
    return dict(rule_version=CHECK_VERSION,state='complete' if entries and len(good) == len(entries) else 'needs_review',citations=entries,
        missing_categories=[KINDS[k] for k in incoming['categories'] if k not in used],
        counts=dict(citation_count=len(entries),valid_count=len(good),pending_count=len(entries)-len(good),
            valid_source_count=len({v['source_key'] for v in good}),report_reference_count=sum(v['reference_type']=='report' for v in entries),
            image_reference_count=sum(v['reference_type']=='image' for v in entries),text_reference_count=sum(v['reference_type']=='text' for v in entries)))


def content(data, origin='execution', issues=None):
    """原始返回保存后派生新版本，固定来源快照和限制随报告保留。"""
    final = next((c for c in reversed(data['calls']) if c['chunk_index'] == 0 and c.get('response')),None)
    response = final['response'] if final else {}; text = response.get('text') or ''
    incoming = data['task']['parameters']['plan']['input']; generated = generation(text,response.get('finish_reason'))
    if not final: generated['state'] = 'not_generated'
    return dict(schema_version=REPORT_VERSION,markdown=text,generation=generated,validation=check(text,incoming),
        categories=incoming['categories'],combination=incoming['combination'],warnings=incoming['warnings'],
        source_reports=[{k:v for k,v in r.items() if k not in ('markdown','evidence')} for r in incoming['reports']],
        source_hash=fingerprint(incoming),detail_revisions={},issues=issues or [],review_status='unreviewed',
        phases=[dict(index=c['chunk_index'],text=c['response']['text']) for c in data['calls'] if c['chunk_index'] > 0 and c.get('response')],
        inspection=dict(rule_version=CHECK_VERSION,origin=origin,checked_at=database_now().isoformat()+'+08:00'))


def message(value):
    """生成与依据核对状态独立，缺可选章节不导致执行失败。"""
    if not value['generation']['complete']:
        return '交叉报告未完整生成；已有内容保留，未自动重试。'+'；'.join(value['issues'] or value['generation']['issues'])
    counts = value['validation']['counts']
    return f'交叉报告已生成；{counts["valid_count"]}/{counts["citation_count"]}处来源定位通过，{counts["pending_count"]}处待核对。定位不代表结论已证实。'


def render(value):
    """受控来源锚点与逐条折叠原文，模型HTML和外部链接不可执行。"""
    text = value['markdown']; prefix = 'CROSSREFERENCE'+uuid4().hex; replacements = []
    for entry in reversed(value['validation']['citations']):
        token = prefix+str(entry['index']); label = html.escape(entry['marker'])
        anchor = f'<a href="#composite-citation-{entry["index"]}">{label}</a>'
        if entry['quote'] is not None:
            markup = '<details class="ui-report-evidence"><summary>查看原文依据 · 1处引用</summary><div class="ui-report-evidence-body"><div class="ui-report-evidence-item">'+anchor+' 原文：“'+html.escape(entry['quote'])+'”</div></div></details>'
        else: markup = anchor+('（待核对）' if not entry['valid'] else '')
        replacements.append((token,markup)); text = text[:entry['start']]+token+text[entry['end']:]
    rendered = safe_render(text)
    for token,markup in replacements: rendered = rendered.replace('<p>'+token+'</p>',markup).replace(token,markup)
    return rendered


def reading(value):
    """复用目录和重点卡布局。"""
    return shared.reading(value,renderer=render)


def markdown(value):
    """导出明确类别、版本与限制的Markdown，不修改模型原返回。"""
    sources = '\n'.join(f'- {r["label"]}：批次 {r["batch_ids"]}；报告版本 {r["version"]}；报告编号 {r["report_id"]}' for r in value['source_reports'])
    return '# 交叉选品分析报告\n\n参与类别：'+value['combination']+'\n\n'+sources+'\n\n'+'\n'.join('- '+s for s in value['warnings'])+'\n\n'+value['markdown']
