"""执行已确认综合计划，复用预算、调用收据与任务日志，零自动付费重试。"""
from dataclasses import replace
from ecommerce.infrastructure.ai.config import load_config
from ecommerce.infrastructure.ai.capabilities import Counter
from ecommerce.infrastructure.ai.client import AIError
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.modules.composite_analysis import planner, report


def save(settings, task_id, issues=None, reporting=report):
    """仅由保存返回追加报告版本；参数：任务和说明；返回：新报告内容。"""
    value = reporting.content(repository.read(settings, task_id), issues=issues)
    repository.save_report(settings, task_id, value)
    return value


def execute(settings, task_id, events, client_factory, planning=planner, reporting=report):
    """执行固定次数计划；参数：配置、任务、日志、工厂；返回：终态。"""
    data = repository.read(settings, task_id); task = data['task']; plan = task['parameters']['plan']
    if data['calls']:
        raise ValueError('任务已有请求记录，禁止自动重发；请核对已有结果或明确另建分析。')
    config = replace(load_config(settings.root), model=task['model']); counter = (Counter if planning is planner else planning.Counter)(settings.root, config, plan['capability'])
    directory = settings.workspace/'output/analysis_receipts'/task_id; directory.mkdir(parents=True, exist_ok=True)
    client = None; issues = []
    def request(index, values, ids, output):
        """先保存请求和预留再联网，返回先落收据；参数：固定请求；返回：无。"""
        nonlocal client
        ok, size = planning.capacity_ok(counter, plan['capability'], values, output)
        if not ok: raise ValueError('实际请求超过冻结容量，未发送；已有组稿保留，不自动加请求。')
        outgoing = planning.transport(settings, task, values) if hasattr(planning, 'transport') else values
        call_id = repository.begin_call(settings, task_id, 'report', index, planning.prompt_view(values), ids,
            size['reserved_tokens'], output_tokens=output, timeout_seconds=plan['timeout_seconds'], messages=values, measurement=size)
        events.emit('analysis_call_started', '开始已确认综合报告请求', details={'analysis_id': task_id, 'call_id': call_id, 'index': index})
        try:
            if client is None: client = client_factory(config, provider=task['provider'], model=task['model'])
            response = client.complete_messages(outgoing, max_tokens=output, capability=plan['capability'], timeout_seconds=plan['timeout_seconds'])
        except AIError:
            message = '接口请求未确认成功，结果或用量未知；未自动重试。'
            repository.finish_call(settings, task_id, call_id, 'unknown', None, message)
            raise ValueError(message) from None
        atomic_json(directory/(call_id+'.json'), response)
        generated = reporting.generation(response.get('text'), response.get('finish_reason'), group=index > 0)
        good = generated['complete']; message = '' if good else '接口未完整返回或缺少章节，原文及用量已保存；未自动付费补写。'
        repository.finish_call(settings, task_id, call_id, 'succeeded' if good else 'invalid', response, message)
        events.emit('analysis_call_finished', '综合报告响应与用量已保存', details={'analysis_id': task_id, 'call_id': call_id, 'status': 'succeeded' if good else 'invalid'})
        if not good: raise ValueError(message)
    try:
        for group in plan['groups']:
            repository.update_task(settings, task_id, message='正在生成综合报告。' if group['index'] == 0 else f'正在整理第{group["index"]}/{len(plan["groups"])}组。')
            request(group['index'], group['messages'], group['source_ids'], group['max_output_tokens'])
            if group['index'] > 0: save(settings, task_id, reporting=reporting)
        if plan['summary']:
            current = repository.read(settings, task_id)
            repository.update_task(settings, task_id, message='组稿已保存，正在生成最终综合报告。')
            request(0, planning.summary_messages(task, current['calls']), plan['source_ids'], plan['report_output_tokens'])
    except ValueError as error:
        issues.append(str(error))
    value = save(settings, task_id, issues, reporting=reporting)
    status = 'succeeded' if value['generation']['complete'] else 'partial' if value['markdown'] or value['phases'] else 'failed'
    repository.update_task(settings, task_id, status=status, active_key=None, owner=None, message=reporting.message(value))
    return status


def recheck(settings, data, report_id, reporting=report):
    """明确本地重查并保留旧版本；参数：固定聚合与预期报告；返回：版本及零AI调用。"""
    if data['task']['status'] in ('queued', 'running'):
        raise ValueError('任务运行中，暂不能重新检查。')
    if not data['report'] or data['report']['id'] != report_id:
        raise ValueError('报告版本已变化，请刷新后重新检查。')
    value = reporting.content(data, origin='local_recheck')
    status = 'succeeded' if value['generation']['complete'] else 'partial' if value['markdown'] or value['phases'] else 'failed'
    version = repository.save_local_report(settings, data['task']['id'], value, report_id, repository.check_inputs(data),
        {'status': status, 'active_key': None, 'owner': None, 'message': reporting.message(value)})
    return {'version': version, 'ai_calls': 0}
