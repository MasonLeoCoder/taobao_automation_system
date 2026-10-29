"""Web与CLI共用的评价分析、校验、恢复与人工修订业务。"""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4
import sqlalchemy as sa
from ecommerce.infrastructure.ai.config import load_config, api_key
from ecommerce.infrastructure.ai.client import create_client, AIError
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.infrastructure.tasks.ownership import owner_alive, process_started_at, atomic_json
from ecommerce.shared.exceptions import TaskBusy
from ecommerce.modules.review_analysis import rules


def identifier(value):
    """校验分析编号；参数：文字；返回：原编号。"""
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('分析编号格式无效。')
    return value


def product_id(value):
    """校验平台商品编号；参数：文字；返回：原编号。"""
    if not isinstance(value, str) or not re.fullmatch('[0-9]{1,25}', value) or int(value) <= 0:
        raise ValueError('商品编号格式无效。')
    return value


def integer(value, low, high, label):
    """校验整数范围；参数：值、边界和名称；返回：整数。"""
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label}须为{low}～{high}的整数。')
    return value


def chunks(items, prompt):
    """按文字上限和条数分批，不截断单条；参数：来源和提示；返回：批次列表。"""
    result, batch = [], []
    for item in items:
        candidate = batch + [item]
        if batch and (len(batch) >= 10 or len(prompt + json.dumps(candidate, ensure_ascii=False)) > 18000):
            result.append(batch)
            batch = []
        batch.append(item)
        if len(prompt + json.dumps(batch, ensure_ascii=False)) > 18000:
            raise ValueError('单条评价及追评过长，首版无法完整发送；未截断原文。')
    if batch:
        result.append(batch)
    return result


def preview(settings, product, batch_id=None):
    """预览资料与预算，不联网；参数：商品及批次；返回：确认页面数据。"""
    product_id(product)
    if batch_id is not None:
        integer(batch_id, 1, 2**63 - 1, '批次编号')
    data = repository.preview(settings, product, batch_id)
    config = load_config(settings.root)
    groups = chunks(data['items'], rules.CLASSIFY)
    reserve = sum(len((rules.CLASSIFY + json.dumps(chunk, ensure_ascii=False)).encode()) + 4096 + 256 for chunk in groups)
    # 报告最多15000字符，按每字符最多4字节及请求开销预留；不是费用承诺。
    reserve += len(rules.REPORT.encode()) + 60000 + 4096 + 256
    data.update(input_hash=fingerprint(data['items']), chunk_count=len(groups), planned_calls=len(groups) + 1,
                default_calls=len(groups) + 3, reserved_tokens=reserve, models=config.models, model=config.model,
                provider=config.provider, key_configured=bool(api_key(config)))
    return data


def start(settings, product, *, batch_id, model, input_hash, max_calls, max_tokens, allow_paid=False, force=False):
    """确认后创建及派发分析；参数：来源、模型、预算和授权；返回：任务编号与复用标记。"""
    if allow_paid is not True:
        raise ValueError('请明确允许本次AI调用费用。')
    if type(force) is not bool:
        raise ValueError('重新分析选项无效。')
    data = preview(settings, product, batch_id)
    config = load_config(settings.root)
    if not isinstance(model, str) or model not in config.models:
        raise ValueError('请选择已配置的模型。')
    create_client(config, model=model)
    if not data['key_configured']:
        raise ValueError('请先在本机AI设置中配置密钥。')
    integer(max_calls, data['planned_calls'], 200, '调用次数上限')
    integer(max_tokens, data['reserved_tokens'], 5000000, '保守token预算')
    parameters = {'batch_size': 10, 'max_output_tokens': 4096, 'max_calls': max_calls, 'max_tokens': max_tokens,
                  'config_file': settings.mysql.credentials_file or str(settings.root / 'config/settings.toml')}
    row, created = repository.create(settings, product, batch_id, rules.snapshot(), parameters, config.provider, model,
                                      rules.VERSION, input_hash, force=force)
    if created:
        launch(settings, row['id'])
    return {'id': row['id'], 'reused': not created}


def read(settings, task_id):
    """读取结果并派生进程存活提示；参数：配置和编号；返回：完整持久数据，不调用AI。"""
    data = repository.read(settings, identifier(task_id))
    task = data['task']
    owner = task.get('owner') or {}
    task['owner_alive'] = owner_alive(owner.get('pid'), owner.get('started_at')) if owner else False
    task['appears_interrupted'] = task['status'] in {'queued', 'running'} and task['owner_alive'] is False
    data['source_by_id'] = {s['id']: s for s in data['sources']}
    data['source_by_review'] = {s['review_id']: s for s in data['sources']}
    for detail in data['details']:
        detail['effective'] = rules.effective(detail)
    from ecommerce.shared.pagination import PAGE_SIZE
    data['detail_locations'] = {str(d['result']['id']): index // PAGE_SIZE + 1 for index, d in enumerate(data['details'])}
    data['report_stale'] = bool(data['report'] and data['report']['content']['detail_revisions'] != {d['id']: d['revision'] for d in data['details']})
    return data


def history_page(settings, product, batch=None):
    """整理当前批次或全部批次记录；参数：配置、商品、批次或all；返回：任务和优先结果，不写库。"""
    product_id(product)
    choices = repository.batch_choices(settings, product)
    if batch == 'all':
        selected = None
    elif batch is None:
        selected = choices[0]['id'] if choices else None
    else:
        integer(batch, 1, 2**63 - 1, '批次编号')
        if batch not in {b['id'] for b in choices}:
            raise LookupError('评价批次不属于当前商品或尚未发布。')
        selected = batch
    tasks = repository.history(settings, product, selected) if selected is not None or batch == 'all' else []
    for task in tasks:
        owner = task.get('owner') or {}
        task['appears_interrupted'] = task['status'] in {'queued', 'running'} and (
            not owner or owner_alive(owner.get('pid'), owner.get('started_at')) is False)
    # 优先最近完整成功的报告；新任务失败或部分完成不遮住此前的成功结果。
    latest_result = next((t for t in tasks if t['status'] == 'succeeded' and t['has_report']), None)
    if latest_result is None and (selected is not None or batch == 'all'):
        latest_result = repository.latest_success(settings, product, selected)
    if latest_result is None:
        latest_result = next((t for t in tasks if t['has_report'] and t['processed_count'] > 0 and t['status'] not in {'queued', 'running'}), None)
    active = next((t for t in tasks if t['status'] in {'queued', 'running'} and not t['appears_interrupted']), None)
    return {'tasks': tasks, 'batches': choices, 'selected_batch': selected, 'all_batches': batch == 'all',
            'latest_result': latest_result, 'active_task': active, 'latest_task': tasks[0] if tasks else None}


def launch(settings, task_id):
    """派发独立进程，只传配置引用；参数：配置与编号；返回：无，失败明确持久保存。"""
    identifier(task_id)
    task = repository.read(settings, task_id)['task']
    path = settings.log_dir / 'analysis_workers'
    path.mkdir(parents=True, exist_ok=True)
    try:
        with (path / f'{task_id}_{uuid4().hex[:8]}.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-m', 'ecommerce.modules.review_analysis.worker',
                '--root', str(settings.root), '--config', task['parameters']['config_file'], '--task-id', task_id],
                cwd=settings.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        with repository.connection(settings, True) as (conn, tables):
            row = repository.task_row(conn, tables, task_id, True)
            if row['status'] == 'queued':
                conn.execute(tables['analysis_tasks'].update().where(tables['analysis_tasks'].c.id == task_id).values(
                    owner={'pid': process.pid, 'started_at': process_started_at(process.pid)}, updated_at=database_now()))
    except OSError:
        repository.update_task(settings, task_id, status='interrupted', active_key=None, owner=None,
                               message='后台进程未能启动；未发起AI请求，可手动继续。')


def resume(settings, task_id, *, allow_paid=False, acknowledge_unknown=False):
    """明确恢复未完成部分；参数：任务、费用及未知请求确认；返回：任务编号。"""
    if allow_paid is not True:
        raise ValueError('继续分析需要明确允许调用费用。')
    data = read(settings, task_id)
    task = data['task']
    # 派发锁与执行锁分离，防两个入口同时恢复；活进程或身份未知均不得重发。
    with shop_lock(settings.workspace / 'storage/analysis_dispatch' / task_id):
        with repository.connection(settings, True) as (conn, tables):
            row = repository.task_row(conn, tables, task_id, True)
            owner = row.get('owner') or {}
            if owner and owner_alive(owner.get('pid'), owner.get('started_at')) is not False:
                raise ValueError('任务进程仍存活或身份无法确认，不能重复启动。')
            if row['status'] == 'succeeded':
                raise ValueError('分析已完成，请查看结果或明确创建新的分析。')
            calls = tables['analysis_calls']
            unknown = conn.scalar(sa.select(sa.func.count()).select_from(calls).where(calls.c.analysis_id == task_id,
                calls.c.status.in_(['inflight', 'unknown'])))
            if unknown and acknowledge_unknown is not True:
                raise ValueError('存在结果未知请求，可能已计费；请明确确认重发风险后继续。')
            conn.execute(tables['analysis_tasks'].update().where(tables['analysis_tasks'].c.id == task_id).values(
                status='queued', active_key=row['fingerprint'], owner=None, message='用户明确继续，等待执行。', updated_at=database_now()))
        launch(settings, task_id)
    return {'id': task_id}


def build_report(settings, task_id, narrative=None, issues=None):
    """从持久明细构建报告，不调用AI；参数：任务、已校验文字、问题；返回：报告版本。"""
    data = repository.read(settings, task_id)
    task = data['task']
    content = {'schema_version': 1, 'statistics': rules.statistics(data['details'], task['rules']),
        'findings': (narrative or {}).get('findings', []), 'suggestions': (narrative or {}).get('suggestions', []),
        'issues': issues or [], 'detail_revisions': {d['id']: d['revision'] for d in data['details']},
        'coverage': {'source_count': task['source_count'], 'processed_count': len(data['details']),
                     'complete': len(data['details']) == task['source_count']},
        'review_status': 'partially_corrected' if any(d['corrections'] for d in data['details']) else 'unreviewed'}
    return repository.save_report(settings, task_id, content)


def correct(settings, task_id, detail_id, *, expected_revision, kind, points, excluded, reason):
    """校验人工修订并追加报告，不产生AI费用；参数：完整结果和原因；返回：新报告版本。"""
    data = read(settings, task_id)
    detail = next((d for d in data['details'] if d['id'] == detail_id), None)
    if detail is None:
        raise LookupError('分析明细不存在。')
    integer(expected_revision, 0, 2**31 - 1, '明细版本')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500 or type(excluded) is not bool:
        raise ValueError('请填写1～500字修改原因，并明确排除状态。')
    source = data['source_by_id'][detail['source_id']]['snapshot']
    value = rules.classification(json.dumps({'reviews': [{'id': source['id'], 'kind': kind, 'points': points}]}), [source])[0]
    value['excluded'] = excluded
    repository.correct(settings, task_id, detail_id, expected_revision, value, reason.strip())
    return {'version': build_report(settings, task_id, issues=['已按人工修订重新统计，原AI综合文字已移除；未重新调用AI。'])}


def recount(settings, task_id):
    """主动重新统计并形成新版本；参数：任务；返回：版本，不联网。"""
    data = read(settings, task_id)
    if data['task']['status'] in {'queued', 'running'}:
        raise ValueError('运行期间不能重新统计。')
    return {'version': build_report(settings, task_id, issues=['从已保存明细重新统计；未调用AI，原综合文字未沿用。'])}


def execute(settings, task_id, events):
    """执行一次恢复轮次；参数：任务和通用日志会话；返回：终态，不自动重试。"""
    data = repository.read(settings, task_id)
    task = data['task']
    config = replace(load_config(settings.root), model=task['model'])
    client = create_client(config, provider=task['provider'], model=task['model'])
    inputs = [s['snapshot'] for s in data['sources']]
    groups = chunks(inputs, task['rules']['classify'])
    complete_ids = {d['result']['id'] for d in data['details']}
    receipts = settings.workspace / 'output/analysis_receipts' / task_id
    receipts.mkdir(parents=True, exist_ok=True)
    # 断库时已落盘的响应先补写，不重发原请求。
    for call in data['calls']:
        if call['status'] != 'inflight':
            continue
        receipt = receipts / (call['id'] + '.json')
        if receipt.exists():
            response = json.loads(receipt.read_text(encoding='utf-8'))
            try:
                if response['finish_reason'] != 'stop':
                    raise ValueError('返回被截断。')
                if call['kind'] == 'classify':
                    parsed = rules.classification(response['text'], [s for s in inputs if s['id'] in call['request']['source_ids']])
                else:
                    context = rules.report_context(repository.read(settings, task_id)['details'], task['rules'])
                    rules.validate_report(response['text'], context)
                    parsed = None
                repository.finish_call(settings, task_id, call['id'], 'succeeded', response, results=parsed)
                complete_ids.update(p['id'] for p in parsed or [])
            except ValueError as error:
                repository.finish_call(settings, task_id, call['id'], 'invalid', response, str(error))
        else:
            repository.finish_call(settings, task_id, call['id'], 'unknown', None, '原进程中断且无可恢复响应，结果和费用未知。')

    def request(kind, index, prompt, ids, validator):
        """先记录再请求并保存收据；参数：用途、分批、提示、编号和校验器；返回：已校验结果。"""
        if len(prompt) > 20000:
            raise ValueError('本批请求过长，未发送。')
        reserve = len(prompt.encode()) + task['parameters']['max_output_tokens'] + 256
        call_id = repository.begin_call(settings, task_id, kind, index, prompt, ids, reserve)
        events.emit('analysis_call_started', '开始分析调用', details={'analysis_id': task_id, 'call_id': call_id, 'kind': kind, 'index': index})
        try:
            response = client.complete(prompt, max_tokens=task['parameters']['max_output_tokens'], require_complete=False)
        except AIError as error:
            repository.finish_call(settings, task_id, call_id, 'unknown', None, str(error))
            raise ValueError(str(error) + ' 调用记录已保留，未自动重试；无完整响应时用量仍记为未知。') from None
        atomic_json(receipts / (call_id + '.json'), response)
        try:
            if response['finish_reason'] != 'stop':
                raise ValueError('AI输出未完整结束，原始返回和用量已保存。')
            value = validator(response['text'])
        except ValueError as error:
            repository.finish_call(settings, task_id, call_id, 'invalid', response, str(error))
            raise
        repository.finish_call(settings, task_id, call_id, 'succeeded', response, results=value if kind == 'classify' else None)
        events.emit('analysis_call_finished', '分析调用已保存', details={'analysis_id': task_id, 'call_id': call_id})
        return value

    issues = []
    narrative = None
    try:
        for index, group in enumerate(groups, 1):
            missing = [s for s in group if s['id'] not in complete_ids]
            if not missing:
                continue
            repository.update_task(settings, task_id, message=f'正在分析第{index}/{len(groups)}批，已保存{len(complete_ids)}条。')
            request('classify', index, task['rules']['classify'] + json.dumps(missing, ensure_ascii=False),
                    [s['id'] for s in missing], lambda text: rules.classification(text, missing))
            complete_ids.update(s['id'] for s in missing)
        current = repository.read(settings, task_id)
        context = rules.report_context(current['details'], task['rules'])
        previous = next((c for c in reversed(current['calls']) if c['kind'] == 'report' and c['status'] == 'succeeded'), None)
        if previous:
            narrative = rules.validate_report(previous['response']['text'], context)
        else:
            narrative = request('report', 0, task['rules']['report'] + json.dumps(context, ensure_ascii=False), [],
                                lambda text: rules.validate_report(text, context))
    except ValueError as error:
        issues.append(str(error))
    build_report(settings, task_id, narrative=narrative, issues=issues)
    status = 'succeeded' if not issues else 'partial' if complete_ids else 'failed'
    repository.update_task(settings, task_id, status=status, active_key=None, owner=None,
        message='分析完成，结果尚未人工复核。' if not issues else '本轮停止，已保存结果可查看；' + issues[0])
    return status
