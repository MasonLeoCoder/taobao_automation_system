"""评价发送计划与用量预留；纯本地计算，不调用AI、不截断评价。"""
import json
import math

VERSION = 2
MAX_INPUT_CHARACTERS = 60000
MAX_OUTPUT_TOKENS = 65536
REPORT_INPUT_CHARACTERS = 32000
REPORT_OUTPUT_TOKENS = 8192
MAX_TASK_TOKENS = 5000000


def encode(value):
    """序列化发送资料；参数：结构化对象；返回：保留中文的紧凑JSON。"""
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def output_limit(items):
    """按观点结构和原文量估算输出预留；参数：整组评价；返回：向上取整的Token上限。"""
    size = sum(len((item['text'] + ''.join(a['text'] for a in item['appends'])).encode('utf-8')) for item in items)
    return max(4096, math.ceil((1024 + len(items) * 192 + size * 2) / 1024) * 1024)


def classify_prompt(instruction, context, references, items):
    """构造实际分类文字；参数：规则、背景、参考主题及评价；返回：完整请求内容。"""
    return instruction + encode({'product_context': context, 'topic_reference': references, 'reviews': items})


def reserve(prompt, output):
    """按UTF-8字节保守估算输入并预留输出；参数：文字及输出上限；返回：程序控制额度。"""
    return len(prompt.encode('utf-8')) + output + 256


def build(items, instruction, context, references, report_instruction, timeout_seconds):
    """按输入与预计输出生成固定分组；参数：来源和规则；返回：可持久化完整计划。"""
    groups, batch = [], []
    if len(encode(context)) > 8000:
        raise ValueError('商品背景过长，无法完整纳入本次报告；未发送评价。')
    for item in items:
        candidate = batch + [item]
        prompt = classify_prompt(instruction, context, references, candidate)
        if batch and (len(prompt) > MAX_INPUT_CHARACTERS or output_limit(candidate) > MAX_OUTPUT_TOKENS):
            groups.append(batch)
            batch = [item]
        else:
            batch = candidate
        if len(classify_prompt(instruction, context, references, batch)) > MAX_INPUT_CHARACTERS or output_limit(batch) > MAX_OUTPUT_TOKENS:
            raise ValueError('单条评价及追评超过本次完整发送限制；原文未截断，未发出请求。')
    if batch:
        groups.append(batch)
    plans = []
    for index, group in enumerate(groups, 1):
        prompt = classify_prompt(instruction, context, references, group)
        output = output_limit(group)
        plans.append({'index': index, 'source_ids': [item['id'] for item in group], 'prompt': prompt,
                      'input_characters': len(prompt), 'max_output_tokens': output, 'reserved_tokens': reserve(prompt, output)})
    report_reserve = len(report_instruction.encode('utf-8')) + REPORT_INPUT_CHARACTERS * 4 + REPORT_OUTPUT_TOKENS + 256
    minimum = sum(group['reserved_tokens'] for group in plans) + report_reserve
    recommended = minimum + max(4096, math.ceil(minimum * 0.1))
    if recommended > MAX_TASK_TOKENS:
        raise ValueError('整批评价的预计用量超过当前单任务限制；未截断原文，未发出请求。')
    return {'version': VERSION, 'context': context, 'topic_reference': references, 'groups': plans,
            'report_reserved_tokens': report_reserve, 'report_output_tokens': REPORT_OUTPUT_TOKENS,
            'report_input_characters': REPORT_INPUT_CHARACTERS, 'minimum_tokens': minimum,
            'recommended_tokens': recommended, 'planned_calls': len(plans) + 1,
            'json_mode': True, 'timeout_seconds': timeout_seconds}


def requests(plan, instruction, sources):
    """从已固定清单复原分类请求并核对来源覆盖；参数：计划、规则和快照；返回：请求列表。"""
    lookup = {source['id']: source for source in sources}
    ids = [uid for group in plan['groups'] for uid in group['source_ids']]
    if len(ids) != len(set(ids)) or set(ids) != set(lookup):
        raise ValueError('发送清单与来源不一致，未发出请求。')
    return [dict(group) for group in plan['groups']]
