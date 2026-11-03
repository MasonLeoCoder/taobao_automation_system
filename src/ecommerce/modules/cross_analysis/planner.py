"""交叉分析消息与完整章节拆分，容量及预算复用综合分析核心。"""
import copy
import re
from pathlib import Path
from ecommerce.modules.composite_analysis import planner as shared
from ecommerce.infrastructure.ai.capabilities import Counter
from .kinds import VERSION, REPORT_VERSION, SECTIONS, REQUIRED, combination, focus

capacity_ok = shared.capacity_ok
prompt_view = shared.prompt_view
encode = shared.encode


def snapshot(kinds):
    """冻结公共提示与当前组合重点，后续文件变化不影响已创建任务。"""
    base = Path(__file__).parent / 'prompts'
    common = (base/'report.txt').read_text(encoding='utf-8')
    task = '\n本次参与类别：' + combination(kinds) + '\n本次重点：\n' + focus(kinds)
    return dict(version=VERSION, schema_version=1, source_format='cross-input-v1', output_format='markdown',
        report_version=REPORT_VERSION, sections=list(SECTIONS), required_sections=list(REQUIRED),
        report=common+task, group=common+task+'\n本次仅执行分组整理，以下任务要求覆盖最终报告的章节输出要求：\n'+(base/'group.txt').read_text(encoding='utf-8'))


def messages(rules, incoming, kind='direct', parts=None):
    """完整输入或分组元信息都带类别和限制，不发送原图、路径和日志。"""
    if kind == 'direct': body = incoming
    else:
        body = dict(product=incoming['product'], categories=incoming['categories'], combination=incoming['combination'],
            warnings=incoming['warnings'], reports=[{k:v for k,v in r.items() if k not in ('markdown','evidence')} for r in incoming['reports']])
        body['group_findings' if kind == 'summary' else 'parts'] = parts
        body['scope_note'] = '完整来源固定保存，片段不是新样本；只使用已提供的组稿，不补写遗漏事实。'
    return [dict(role='system', content=rules['group' if kind == 'group' else 'report']), dict(role='user', content=encode(body))]


def units(incoming):
    """优先完整Markdown章节，单章超容由公共核心带坐标拆片，全文不丢字。"""
    result = []
    for source in incoming['reports']:
        text = source['markdown']; starts = [m.start() for m in re.finditer(r'^#{1,3}\s+', text, re.M)]
        starts = sorted(set([0]+starts+[len(text)]))
        for start,end in zip(starts, starts[1:]):
            result.append(dict(id=source['report_id'], report_id=source['report_id'], kind=source['kind'], type='report_fragment',
                start=start, end=end, source_length=len(text), text=text[start:end]))
        result.extend(dict(copy.deepcopy(e), kind=source['kind'], report_id=source['report_id'], type='original') for e in source['evidence'])
    return result


def plan(root, config, model, incoming, rules, output_tokens=None):
    """共享直接或N＋1规划；参数：固定来源及设置；返回：不联网计划。"""
    return shared.plan(root, config, model, incoming, rules, output_tokens, messages_builder=messages, units_builder=units)


def summary_messages(task, calls):
    """汇总只读实际保存的组稿，固定类别和限制，不重新发送全部来源。"""
    return messages(task['rules'], task['parameters']['plan']['input'], 'summary',
        [dict(index=c['chunk_index'], report_ids=c['request']['source_ids'], findings=c['response']['text'])
         for c in calls if c['chunk_index'] > 0 and c.get('response')])
