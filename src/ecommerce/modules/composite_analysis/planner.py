"""综合报告容量规划：能一次则一次，超限才N组加一次汇总。"""
import copy
import math
from pathlib import Path
from ecommerce.infrastructure.ai.capabilities import capability, Counter, cost_ceiling
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.modules.question_analysis.planner import encode, capacity_ok, prompt_view, MAX_TASK_TOKENS
from ecommerce.modules.composite_analysis.store import VERSION


def snapshot():
    """冻结主报告及组稿提示；参数：无；返回：规则字典。"""
    base = Path(__file__).parent / 'prompts'
    return {'version': VERSION, 'schema_version': 1, 'source_format': 'composite-input-v1', 'output_format': 'markdown',
            'report': (base/'report.txt').read_text(encoding='utf-8'), 'group': (base/'group.txt').read_text(encoding='utf-8')}


def messages(rules, incoming, kind='direct', parts=None):
    """组织实际消息；参数：规则、冻结输入、用途与分组/返回；返回：system/user消息。"""
    if kind == 'direct':
        body = incoming
    else:
        metadata = [{k: v for k, v in report.items() if k not in ('markdown', 'evidence')} for report in incoming['reports']]
        body = {'product': incoming['product'], 'reports': metadata,
                'group_findings' if kind == 'summary' else 'parts': parts,
                'scope_note': '完整输入已固定保存；片段不是新样本，未引用不代表不存在。摘要与原文分开，待核对项保留。'}
    return [{'role': 'system', 'content': rules['group' if kind == 'group' else 'report']}, {'role': 'user', 'content': encode(body)}]


def units(incoming):
    """将报告段落与去重原文分为有归属单元；参数：输入；返回：单元，不删字。"""
    result = []
    for report in incoming['reports']:
        text = report['markdown']; start = 0
        for part in text.splitlines(keepends=True):
            result.append({'id': report['report_id'], 'kind': report['kind'], 'type': 'report_fragment',
                           'start': start, 'end': start+len(part), 'source_length': len(text), 'text': part})
            start += len(part)
        for evidence in report['evidence']:
            result.append(dict(copy.deepcopy(evidence), kind=report['kind'], type='original', report_id=report['report_id']))
    return result


def plan(root, config, model, incoming, rules, output_tokens=None):
    """完整预检消息、分片与汇总上界；参数：配置及固定输入；返回：不联网计划。"""
    profile = capability(config, model); counter = Counter(root, config, profile)
    output = config.report_output_tokens if output_tokens is None else output_tokens
    if type(output) is not int or not 512 <= output <= profile['max_output_tokens']:
        raise ValueError('报告输出上限须为512～当前模型最大输出量。')
    group_output = min(config.group_output_tokens, output, profile['max_output_tokens'])
    whole = messages(rules, incoming); fits, measurement = capacity_ok(counter, profile, whole, output)
    groups = []; summary = None
    def add(values, ids, kind, out):
        """冻结一个实际请求；参数：消息、来源、用途、输出；返回：无。"""
        ok, size = capacity_ok(counter, profile, values, out)
        if not ok:
            raise ValueError('请求超过模型容量或网络保护，未发送。')
        groups.append({'index': 0 if kind == 'direct' else len(groups)+1, 'kind': kind,
            'source_ids': list(dict.fromkeys(ids)), 'messages': values, 'prompt': prompt_view(values),
            'max_output_tokens': out, 'measurement': size, 'reserved_tokens': size['reserved_tokens'], 'request_hash': fingerprint(values)})
        if len(groups)>199 and kind=='group':
            raise ValueError('计划超过单任务200次保护，未发送；不静默截断。')
    if fits:
        add(whole, [v['report_id'] for v in incoming['reports']], 'direct', output)
    else:
        packed = []
        def unit_fits(unit):
            """单元容量检查；参数：单元；返回：是否可发。"""
            return capacity_ok(counter, profile, messages(rules, incoming, 'group', [unit]), group_output)[0]
        for original in units(incoming):
            fragments = []
            if unit_fits(original):
                fragments = [original]
            else:
                text = original['text']; start = 0
                while start < len(text):
                    lo = start+1; hi = len(text); best = start
                    while lo <= hi:
                        mid = (lo+hi)//2
                        candidate = dict(original, text=text[start:mid], fragment={'start': start, 'end': mid, 'source_length': len(text)})
                        if unit_fits(candidate): best = mid; lo = mid+1
                        else: hi = mid-1
                    if best == start:
                        raise ValueError('最小片段及必要背景也无法容纳，请选择更大容量模型；未截断或发送。')
                    fragments.append(dict(original, text=text[start:best], fragment={'start': start, 'end': best, 'source_length': len(text)})); start = best
                    if len(fragments) > 199:
                        raise ValueError('单项分片超过请求保护，未发送。')
            for unit in fragments:
                if packed and not capacity_ok(counter, profile, messages(rules, incoming, 'group', packed+[unit]), group_output)[0]:
                    add(messages(rules, incoming, 'group', packed), [v.get('report_id', v['id']) for v in packed], 'group', group_output); packed = []
                packed.append(unit)
        if packed:
            add(messages(rules, incoming, 'group', packed), [v.get('report_id', v['id']) for v in packed], 'group', group_output)
        placeholders = [{'index': g['index'], 'findings': ''} for g in groups]
        base = counter.count(messages(rules, incoming, 'summary', placeholders))
        # 同现有流程：本地分词器仍保守预留；无分词数据不声称精确容量。
        bound = base + sum(g['max_output_tokens'] for g in groups)*(2 if counter.tool else 1024)
        margin = max(2048, math.ceil(bound*.2))
        summary = {'index': 0, 'kind': 'summary', 'max_output_tokens': output, 'input_bound': bound,
                   'safety_tokens': margin, 'reserved_tokens': bound+output+margin}
        if summary['reserved_tokens'] > profile['context_tokens']:
            raise ValueError('分组后的汇总容量也不足，未发送；不自动增加层级或调用次数，请调整模型或输出上限。')
    calls = len(groups)+int(summary is not None)
    minimum = sum(g['reserved_tokens'] for g in groups)+(summary['reserved_tokens'] if summary else 0)
    if calls > 200 or minimum > MAX_TASK_TOKENS:
        raise ValueError('计划超过单任务200次或500万Token保护，未发送。')
    input_bound = sum(g['measurement']['input_tokens']+g['measurement']['safety_tokens'] for g in groups)+(summary['input_bound']+summary['safety_tokens'] if summary else 0)
    return {'version': 1, 'mode': 'direct' if summary is None else 'grouped', 'context': incoming['product'],
        'input': copy.deepcopy(incoming), 'capability': profile, 'counter': counter.identity, 'groups': groups, 'summary': summary,
        'report_output_tokens': output, 'group_output_tokens': group_output, 'timeout_seconds': config.analysis_timeout_seconds,
        'planned_calls': calls, 'minimum_tokens': minimum, 'recommended_tokens': min(MAX_TASK_TOKENS, minimum+max(256, math.ceil(minimum/20))),
        'whole_measurement': measurement, 'source_ids': [v['report_id'] for v in incoming['reports']],
        'estimated_cost': cost_ceiling(profile, input_bound, sum(g['max_output_tokens'] for g in groups)+(output if summary else 0))}


def summary_messages(task, calls):
    """汇总已保存组稿，不重发全量原文；参数：任务、完整组返回；返回：实际消息。"""
    return messages(task['rules'], task['parameters']['plan']['input'], 'summary',
                    [{'index': c['chunk_index'], 'report_ids': c['request']['source_ids'], 'findings': c['response']['text']} for c in calls])
