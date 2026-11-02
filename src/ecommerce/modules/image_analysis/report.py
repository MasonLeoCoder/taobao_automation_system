"""图片报告完整性与定位检查；不宣称OCR、语义或性能通过。"""
import html
import re
from uuid import uuid4
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.modules.question_analysis.report import render as safe_render
from ecommerce.modules.composite_analysis.report import reading as shared_reading

SECTIONS=('总体判断','主图分析','详情分析','联合检查','选品线索','展示与改进建议')
MARKER=re.compile(r'【((?:主图|详情)[1-9]\d*(?:[｜|]片段[1-9]\d*)?)】')


def generation(text,finish_reason,group=False):
    """只判断可读章节及完整结束，不用定位检查阻止报告阅读。"""
    text=text if isinstance(text,str) else ''; headings=re.findall(r'^\s*#{1,3}\s+(.+?)\s*$',text,re.M)
    missing=[] if group else [name for name in SECTIONS if not any(name in h for h in headings)]
    issues=(['接口未完整结束，未自动续写'] if finish_reason!='stop' else [])+['缺少章节：'+s for s in missing]
    if not text.strip(): issues.append('尚无可读报告')
    return dict(complete=not issues,state='available' if not issues else 'incomplete',finish_reason=finish_reason,missing_sections=missing,issues=issues)


def lookup(messages):
    """只收集确实发送过的描述标记；返回：定位映射。"""
    result={}
    for message in messages:
        if isinstance(message['content'],list):
            for frame in message['content']:
                if frame['type']=='image_ref':
                    for origin in frame['origins']:
                        result[origin['label']]=dict(origin,sha256=frame['sha256'])
    return result


def check(text,sent):
    """核对报告图号是否出现在实际提交范围，绝不替代看图语义判断。"""
    entries=[]
    for match in MARKER.finditer(text):
        key=match[1].replace('|','｜'); source=sent.get(key)
        # 完整原图标记也允许定位多片段原图，不能据此声称看清所有文字。
        if source is None and '｜' not in key: source=next((v for k,v in sent.items() if k.startswith(key+'｜')),None)
        entries.append(dict(index=len(entries)+1,marker=match[0],key=key,start=match.start(),end=match.end(),valid=source is not None,
            source=source,reason='图号属于本次提交；文字、宣称真实性及分析含义仍需人工核对' if source else '未找到本次已提交的对应图号'))
    good=sum(v['valid'] for v in entries)
    return dict(rule_version='images-local-check-v1',state='complete' if entries and good==len(entries) else 'needs_review',citations=entries,
        counts=dict(citation_count=len(entries),valid_count=good,pending_count=len(entries)-good,
            valid_source_count=len({v['source']['media_id'] for v in entries if v['valid']})))


def content(data,origin='execution',issues=None):
    """从持久返回整理报告，不重新请求模型，不修改旧报告。"""
    final=next((v for v in reversed(data['calls']) if v['chunk_index']==0 and v.get('response')),None)
    response=final['response'] if final else {}; text=response.get('text') or ''; sent={}
    for call in data['calls']:
        if call.get('response'): sent.update(lookup(call['request'].get('messages') or []))
    incoming=data['task']['parameters']['plan']['input']; generated=generation(text,response.get('finish_reason'))
    if not final: generated['state']='not_generated'
    return dict(schema_version='images-report-v1',markdown=text,generation=generated,validation=check(text,sent),
        source_images=incoming['images'],scope=incoming['scope'],source_hash=fingerprint(incoming),detail_revisions={},
        phases=[dict(index=v['chunk_index'],text=v['response']['text']) for v in data['calls'] if v['chunk_index']>0 and v.get('response')],
        issues=issues or [],inspection=dict(origin=origin,rule_version='images-local-check-v1',checked_at=database_now().isoformat()+'+08:00'),review_status='unreviewed')


def message(value):
    """报告生成与图号定位状态分开说明。"""
    if not value['generation']['complete']: return '图片报告未完整生成；已保存返回，未自动付费补写。'+'；'.join(value['issues'] or value['generation']['issues'])
    counts=value['validation']['counts']
    return f'图片报告已生成；{counts["valid_count"]}/{counts["citation_count"]}处图号可定位。'+('未识别到图号，依据待核对。' if not counts['citation_count'] else '')+'定位不代表宣称或结论已证实。'


def render(value):
    """安全Markdown后插入受控本页图片链接，外部链接及HTML不可执行。"""
    text=value['markdown']; replacements=[]; token='IMAGECITATION'+uuid4().hex
    for entry in reversed(value['validation']['citations']):
        placeholder=token+str(entry['index']); label=html.escape(entry['marker'])
        markup=f'<a href="#image-source-{entry["source"]["media_id"]}">{label}</a>' if entry['valid'] else label+'（图号待核对）'
        replacements.append((placeholder,markup)); text=text[:entry['start']]+placeholder+text[entry['end']:]
    result=safe_render(text)
    for placeholder,markup in replacements: result=result.replace(placeholder,markup)
    return result


def reading(value):
    """复用报告目录阅读布局，仅替换图片定位渲染。"""
    return shared_reading(value,renderer=render)
