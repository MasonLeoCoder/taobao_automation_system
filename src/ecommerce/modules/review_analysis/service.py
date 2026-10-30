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
from ecommerce.modules.review_analysis import rules, planning, validation, v4, v4_runtime


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


def preview_legacy(settings, product, batch_id=None):
    """预览资料与预算，不联网；参数：商品及批次；返回：确认页面数据。"""
    product_id(product)
    if batch_id is not None:
        integer(batch_id, 1, 2**63 - 1, '批次编号')
    data = repository.preview(settings, product, batch_id)
    config = load_config(settings.root)
    snapshot = rules.snapshot()
    plan = planning.build(data['items'], snapshot['classify'], data['context'], rules.topic_reference(data['context']),
                          snapshot['report'], config.analysis_timeout_seconds)
    outgoing = planning.requests(plan, snapshot['classify'], data['items'])
    data.update(input_hash=fingerprint(data['items']), plan_hash=fingerprint({'plan': plan, 'rules': snapshot}),
                plan=plan, outgoing=outgoing, report_template=snapshot['report'], chunk_count=len(plan['groups']),
                planned_calls=plan['planned_calls'], default_calls=plan['planned_calls'],
                reserved_tokens=plan['minimum_tokens'], default_tokens=plan['recommended_tokens'], models=config.models, model=config.model,
                provider=config.provider, key_configured=bool(api_key(config)))
    return data


def preview(settings, product, batch_id=None):
    """新任务默认使用冻结V4契约；参数：配置、商品、批次；返回：完整计划，不联网。"""
    product_id(product)
    if batch_id is not None:integer(batch_id,1,2**63-1,'批次编号')
    data=repository.preview(settings,product,batch_id);config=load_config(settings.root);snapshot=v4.snapshot()
    plan=v4.plan(data['items'],snapshot,data['context'],config.analysis_timeout_seconds)
    data.update(input_hash=fingerprint(data['items']),plan_hash=fingerprint({'plan':plan,'rules':snapshot}),
        plan=plan,outgoing=plan['groups'],rule_snapshot=snapshot,rule_version=v4.VERSION,
        report_template=snapshot['report'],chunk_count=len(plan['groups']),planned_calls=plan['planned_calls'],
        default_calls=plan['planned_calls'],reserved_tokens=plan['minimum_tokens'],default_tokens=plan['recommended_tokens'],
        models=config.models,model=config.model,provider=config.provider,key_configured=bool(api_key(config)))
    return data


def start(settings, product, *, batch_id, model, input_hash, max_calls=None, max_tokens=None, allow_paid=False, force=False, plan_hash=None):
    """确认后创建及派发分析；参数：来源、模型、预算和授权；返回：任务编号与复用标记。"""
    if allow_paid is not True:
        raise ValueError('请明确允许本次AI调用费用。')
    if type(force) is not bool:
        raise ValueError('重新分析选项无效。')
    data = preview(settings, product, batch_id)
    if plan_hash is not None and plan_hash != data['plan_hash']:
        raise ValueError('发送计划或分析要求已变化，请刷新预览后再开始。')
    config = load_config(settings.root)
    if not isinstance(model, str) or model not in config.models:
        raise ValueError('请选择已配置的模型。')
    create_client(config, model=model)
    if not data['key_configured']:
        raise ValueError('请先在本机AI设置中配置密钥。')
    automatic = max_tokens is None and max_calls is None
    max_calls = data['default_calls'] if max_calls is None else max_calls
    max_tokens = data['default_tokens'] if max_tokens is None else max_tokens
    integer(max_calls, data['planned_calls'], 200, '调用次数上限')
    if type(max_tokens) is int and max_tokens < data['reserved_tokens']:
        raise ValueError(f'本次AI用量上限不足：至少需预留{data["reserved_tokens"]}，当前设置{max_tokens}，还差{data["reserved_tokens"]-max_tokens}；未发送评价。')
    integer(max_tokens, data['reserved_tokens'], planning.MAX_TASK_TOKENS, '本次AI用量上限')
    parameters = {'plan': data['plan'], 'budget_version': 2, 'budget_mode': 'auto' if automatic else 'manual',
                  'budget_history': [], 'max_output_tokens': planning.MAX_OUTPUT_TOKENS, 'max_calls': max_calls, 'max_tokens': max_tokens,
                  'config_file': settings.mysql.credentials_file or str(settings.root / 'config/settings.toml')}
    row, created = repository.create(settings, product, batch_id, data.get('rule_snapshot',rules.snapshot()), parameters, config.provider, model,
                                      data.get('rule_version',rules.VERSION), input_hash, force=force)
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
        detail['effective'] = v4.effective(detail) if task['rule_version']==v4.VERSION else rules.effective(detail)
    from ecommerce.shared.pagination import PAGE_SIZE
    data['detail_locations'] = {str(d['result']['id']): index // PAGE_SIZE + 1 for index, d in enumerate(data['details'])}
    data['report_stale'] = bool(data['report'] and data['report']['content']['detail_revisions'] != {d['id']: d['revision'] for d in data['details']})
    data['budget'] = repository.budget_status(task['parameters'], data['calls'])
    if task['rule_version']==v4.VERSION:
        from ecommerce.modules.review_analysis.v4_view import enrich
        return enrich(settings,data)
    try:
        data['resume_budget'] = remaining_budget(data)
    except ValueError as error:
        data['resume_budget'] = {'blocked':str(error),'required_calls':0,'suggested_tokens':task['parameters']['max_tokens'],
            'suggested_calls':task['parameters']['max_calls'],'supplement':{'source_ids':[],'groups':[]},'plan_hash':''}
    plan = task['parameters'].get('plan')
    data['outgoing'] = planning.requests(plan, task['rules']['classify'], [s['snapshot'] for s in data['sources']]) if plan else []
    done = {detail['result']['id'] for detail in data['details']}
    data['validation_issues'] = validation.pending_issues(task['parameters'], done)
    data['normalizations'] = [change for record in task['parameters'].get('validation_history',[]) for change in record['adjustments']]
    data['needs_local_recovery'] = needs_local_recovery(data)
    return data


def needs_local_recovery(data):
    """检查有原响应但尚未做逐条校验的失败调用；参数：任务；返回：是否应先本地恢复。"""
    policy=v4.POLICY if data['task']['rule_version']==v4.VERSION else validation.POLICY
    checked = {record['call_id'] for record in data['task']['parameters'].get('validation_history',[]) if record['policy']==policy}
    done = {detail['result']['id'] for detail in data['details']}
    return any(call['kind']=='classify' and call['status'] not in {'inflight','unknown'} and call['response'] is not None
               and call['id'] not in checked and any(uid not in done for uid in call['request']['source_ids']) for call in data['calls'])


def continuation(data):
    """只为未完成评价构建补处理预览，原计划不变；参数：任务；返回：完整补处理方案。"""
    if data['task']['rule_version']==v4.VERSION:return v4_runtime.continuation(data)
    task, parameters = data['task'], data['task']['parameters']
    done = {detail['result']['id'] for detail in data['details']}
    pending = [source['snapshot'] for source in sorted(data['sources'],key=lambda source:source['position']) if source['review_id'] not in done]
    plan = parameters.get('plan')
    groups = []
    if pending and plan:
        instruction = task['rules']['classify'] + validation.SUPPLEMENT
        proposed = planning.build(pending,instruction,plan['context'],plan['topic_reference'],task['rules']['report'],plan['timeout_seconds'])
        groups = planning.requests(proposed,instruction,pending)
    elif pending:
        for index,group in enumerate(chunks(pending,task['rules']['classify']),1):
            prompt = task['rules']['classify'] + json.dumps(group,ensure_ascii=False)
            groups.append({'index':index,'source_ids':[item['id'] for item in group],'prompt':prompt,
                           'max_output_tokens':parameters['max_output_tokens'],'reserved_tokens':planning.reserve(prompt,parameters['max_output_tokens'])})
    report_needed = not any(call['kind']=='report' and call['status']=='succeeded' for call in data['calls'])
    return {'version':1,'policy':validation.POLICY,'source_ids':[item['id'] for item in pending],
            'groups':groups,'report_needed':report_needed,'required_calls':len(groups)+int(report_needed)}


def remaining_budget(data):
    """估算继续当前任务所需额度；参数：持久任务及明细；返回：剩余预留和建议总上限。"""
    if data['task']['rule_version']==v4.VERSION:return v4_runtime.remaining_budget(data)
    task, parameters = data['task'], data['task']['parameters']
    supplement = continuation(data)
    if parameters.get('plan'):
        plan = parameters['plan']
        report_reserve = plan['report_reserved_tokens']
    else:
        report_reserve = len(task['rules']['report'].encode()) + 60000 + parameters['max_output_tokens'] + 256
    tokens = sum(group['reserved_tokens'] for group in supplement['groups'])
    if supplement['report_needed']:
        tokens += report_reserve
    budget = repository.budget_status(parameters, data['calls'])
    minimum = budget['used_tokens'] + tokens
    return {'minimum_tokens': minimum, 'required_calls': supplement['required_calls'], 'supplement':supplement,
            'plan_hash':fingerprint(supplement),
            'suggested_tokens': max(parameters['max_tokens'], minimum + max(4096, (tokens + 9) // 10)),
            'suggested_calls': max(parameters['max_calls'], len(data['calls']) + supplement['required_calls'])}


def recover_saved(settings, task_id):
    """本地恢复已有返回并追加统计，不派发进程或调用AI；参数：任务编号；返回：恢复数量及问题。"""
    identifier(task_id)
    with shop_lock(settings.workspace / 'storage/analysis_dispatch' / task_id):
        data = read(settings,task_id)
        owner = data['task'].get('owner') or {}
        if owner and owner_alive(owner.get('pid'),owner.get('started_at')) is not False:
            raise ValueError('分析进程仍存活或身份无法确认，不能同时本地恢复。')
        if data['task']['status']=='succeeded':
            raise ValueError('任务已经完成，不重复恢复已有结果。')
        if data['task']['rule_version']==v4.VERSION:return v4_runtime.recover_saved(settings,task_id)
        by_id = {source['review_id']:source['snapshot'] for source in data['sources']}
        added, recorded = [], False
        for call in data['calls']:
            if call['kind']!='classify' or call['response'] is None or call['status'] in {'inflight','unknown'}:
                continue
            source_ids = call['request']['source_ids']
            assessment = validation.assess(call['response']['text'],[by_id[uid] for uid in source_ids],finish_reason=call['response']['finish_reason'])
            outcome = repository.recover_call(settings,task_id,call['id'],assessment,fingerprint(call['response']))
            added.extend(outcome['added_ids']); recorded = recorded or outcome['recorded']
        current = read(settings,task_id)
        count = len(current['details'])
        if recorded or current['report_stale']:
            messages = [f'本地校验已保存{count}/{current["task"]["source_count"]}条；其余待复核，本操作未调用AI。']
            build_report(settings,task_id,issues=messages)
        if recorded or added or current['report_stale'] or (current['task']['status']=='failed' and count):
            repository.update_task(settings,task_id,status='partial' if count else 'failed',active_key=None,owner=None,
                message=f'本地校验已保存{count}/{current["task"]["source_count"]}条，{current["task"]["source_count"]-count}条待处理；总报告未完成，未调用AI。')
        return {'id':task_id,'added_count':len(added),'processed_count':count,'pending_count':current['task']['source_count']-count,
                'issue_count':len(current['validation_issues']),'ai_calls':0}


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


def resume(settings, task_id, *, allow_paid=False, acknowledge_unknown=False, max_tokens=None, max_calls=None, plan_hash=None):
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
            if row['status'] == 'succeeded' and not (row['rule_version']==v4.VERSION and not data['report']['content'].get('narrative')):
                raise ValueError('分析已完成，请查看结果或明确创建新的分析。')
            calls = tables['analysis_calls']
            unknown = conn.scalar(sa.select(sa.func.count()).select_from(calls).where(calls.c.analysis_id == task_id,
                calls.c.status.in_(['inflight', 'unknown'])))
            if unknown and acknowledge_unknown is not True:
                raise ValueError('存在结果未知请求，可能已计费；请明确确认重发风险后继续。')
            current = {'task': row}
            for key, name in [('calls', 'analysis_calls'), ('details', 'analysis_details'), ('sources', 'analysis_sources')]:
                table = tables[name]
                current[key] = [dict(value) for value in conn.execute(sa.select(table).where(table.c.analysis_id == task_id)).mappings()]
            if needs_local_recovery(current):
                raise ValueError('已有可利用的AI返回，请先执行“重新校验已有返回”；本次未发起付费请求。')
            required = remaining_budget(current)
            if plan_hash is not None and plan_hash != required['plan_hash']:
                raise ValueError('剩余评价或补处理计划已变化，请刷新预览后再确认费用。')
            parameters = dict(row['parameters'])
            tokens = parameters['max_tokens'] if max_tokens is None else max_tokens
            count = parameters['max_calls'] if max_calls is None else max_calls
            integer(tokens, parameters['max_tokens'], planning.MAX_TASK_TOKENS, '追加后的AI用量上限')
            integer(count, parameters['max_calls'], 200, '追加后的调用次数上限')
            if tokens < required['minimum_tokens'] or count < len(current['calls']) + required['required_calls']:
                raise ValueError(f'继续所需额度不足：总用量上限至少{required["minimum_tokens"]}，总调用次数至少{len(current["calls"])+required["required_calls"]}；未启动新请求。')
            if tokens != parameters['max_tokens'] or count != parameters['max_calls']:
                history = parameters.get('budget_history', [])
                parameters['budget_history'] = [*history, {'time': database_now().isoformat() + '+08:00',
                    'before': {'max_tokens': parameters['max_tokens'], 'max_calls': parameters['max_calls']},
                    'after': {'max_tokens': tokens, 'max_calls': count}, 'reason': '用户确认追加额度并继续未完成部分'}]
                parameters.update(max_tokens=tokens, max_calls=count)
            supplement = {**required['supplement'],'id':uuid4().hex,'time':database_now().isoformat()+'+08:00',
                          'plan_hash':required['plan_hash']}
            parameters['supplement_history'] = [*parameters.get('supplement_history',[]),supplement]
            parameters['active_supplement'] = supplement
            t = tables['analysis_tasks']
            if conn.scalar(sa.select(sa.func.count()).select_from(t).where(t.c.active_key == row['fingerprint'], t.c.id != task_id)):
                raise ValueError('已有相同资料与规则的分析正在运行，请先查看该任务。')
            conn.execute(tables['analysis_tasks'].update().where(tables['analysis_tasks'].c.id == task_id).values(
                status='queued', active_key=row['fingerprint'], owner=None, parameters=parameters,
                message='用户明确继续，等待执行。', updated_at=database_now()))
        launch(settings, task_id)
    return {'id': task_id}


def build_report(settings, task_id, narrative=None, issues=None):
    """从持久明细构建报告，不调用AI；参数：任务、已校验文字、问题；返回：报告版本。"""
    data = repository.read(settings, task_id)
    task = data['task']
    if task['rule_version']==v4.VERSION:return v4_runtime.build_report(settings,task_id,narrative,issues)
    content = {'schema_version': 2 if task['parameters'].get('plan') else 1, 'statistics': rules.statistics(data['details'], task['rules']),
        'findings': (narrative or {}).get('findings', []), 'suggestions': (narrative or {}).get('suggestions', []),
        'issues': issues or [], 'detail_revisions': {d['id']: d['revision'] for d in data['details']},
        'coverage': {'source_count': task['source_count'], 'processed_count': len(data['details']),
                     'unprocessed_count': task['source_count'] - len(data['details']),
                     'complete': len(data['details']) == task['source_count'],
                     'report_complete': narrative is not None and len(data['details']) == task['source_count']},
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
    if task['rule_version']==v4.VERSION:return v4_runtime.execute(settings,task_id,events,create_client)
    config = replace(load_config(settings.root), model=task['model'])
    client = create_client(config, provider=task['provider'], model=task['model'])
    inputs = [s['snapshot'] for s in data['sources']]
    plan = task['parameters'].get('plan')
    active_supplement = task['parameters'].get('active_supplement')
    if active_supplement:
        groups = active_supplement['groups']
    elif plan:
        groups = planning.requests(plan, task['rules']['classify'], inputs)
    else:
        # 旧任务继续沿用原10条/18000字符规则和已保存的提示词，不重排来源。
        groups = [{'index': index, 'source_ids': [item['id'] for item in group],
                   'max_output_tokens': task['parameters']['max_output_tokens']}
                  for index, group in enumerate(chunks(inputs, task['rules']['classify']), 1)]
    complete_ids = {d['result']['id'] for d in data['details']}
    receipts = settings.workspace / 'output/analysis_receipts' / task_id
    receipts.mkdir(parents=True, exist_ok=True)
    receipt_issues = []
    # 断库时已落盘的响应先补写，不重发原请求。
    for call in data['calls']:
        if call['status'] != 'inflight':
            continue
        receipt = receipts / (call['id'] + '.json')
        if receipt.exists():
            response = json.loads(receipt.read_text(encoding='utf-8'))
            if call['kind'] == 'classify':
                assessment = validation.assess(response['text'],[s for s in inputs if s['id'] in call['request']['source_ids']],
                                               finish_reason=response['finish_reason'])
                message = assessment['issues'][0]['message'] if assessment['issues'] else ''
                repository.finish_call(settings,task_id,call['id'],'invalid' if assessment['issues'] else 'succeeded',response,
                                       message,assessment=assessment)
                complete_ids.update(result['id'] for result in assessment['results'])
                if assessment['issues']:
                    receipt_issues.append('已有返回含待复核问题，合格评价已保存；未自动重试。')
                continue
            try:
                if response['finish_reason'] != 'stop':
                    raise ValueError('返回被截断。')
                context = rules.report_context(repository.read(settings, task_id)['details'], task['rules'],
                    source_count=task['source_count'], product_context=plan['context'] if plan else None)
                rules.validate_report(response['text'], context)
                parsed = None
                repository.finish_call(settings, task_id, call['id'], 'succeeded', response, results=parsed)
                complete_ids.update(p['id'] for p in parsed or [])
            except ValueError as error:
                repository.finish_call(settings, task_id, call['id'], 'invalid', response, str(error))
        else:
            repository.finish_call(settings, task_id, call['id'], 'unknown', None, '原进程中断且无可恢复响应，结果和费用未知。')

    def request(kind, index, prompt, ids, validator, output):
        """先记录再请求并保存收据；参数：用途、分批、提示、编号和校验器；返回：已校验结果。"""
        if len(prompt) > (planning.MAX_INPUT_CHARACTERS if plan else 20000):
            raise ValueError('本批请求过长，未发送。')
        reserve = planning.reserve(prompt, output)
        call_id = repository.begin_call(settings, task_id, kind, index, prompt, ids, reserve,
            output_tokens=output, json_mode=bool(plan), timeout_seconds=plan['timeout_seconds'] if plan else None)
        events.emit('analysis_call_started', '开始分析调用', details={'analysis_id': task_id, 'call_id': call_id, 'kind': kind, 'index': index})
        try:
            options = {'max_tokens': output, 'require_complete': False}
            if plan:
                options.update(json_mode=True, timeout_seconds=plan['timeout_seconds'])
            response = client.complete(prompt, **options)
        except AIError as error:
            repository.finish_call(settings, task_id, call_id, 'unknown', None, str(error))
            raise ValueError(str(error) + ' 调用记录已保留，未自动重试；无完整响应时用量仍记为未知。') from None
        atomic_json(receipts / (call_id + '.json'), response)
        if kind == 'classify':
            assessment = validator(response['text']) if response['finish_reason']=='stop' else validation.assess('',[],finish_reason=response['finish_reason'])
            assessment['source_ids'] = ids
            message = assessment['issues'][0]['message'] if assessment['issues'] else ''
            repository.finish_call(settings,task_id,call_id,'invalid' if assessment['issues'] else 'succeeded',response,
                                   message,assessment=assessment)
            complete_ids.update(result['id'] for result in assessment['results'])
            if assessment['issues']:
                raise ValueError(f'本次返回已保存{len(assessment["results"])}条合格评价，其他问题已列出；{message}')
            events.emit('analysis_call_finished','逐条校验已保存',details={'analysis_id':task_id,'call_id':call_id})
            return assessment['results']
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
        if receipt_issues:
            raise ValueError(receipt_issues[0])
        for group in groups:
            index = group['index']
            missing = [s for s in inputs if s['id'] in group['source_ids'] and s['id'] not in complete_ids]
            if not missing:
                continue
            repository.update_task(settings, task_id, message=f'正在分析第{index}/{len(groups)}批，已保存{len(complete_ids)}条。')
            if plan and len(missing) != len(group['source_ids']):
                raise ValueError('已保存明细与固定发送分组不一致，未重复发送已完成评价。')
            prompt = group['prompt'] if plan else task['rules']['classify'] + json.dumps(missing, ensure_ascii=False)
            request('classify', index, prompt, [s['id'] for s in missing],
                    lambda text: validation.assess(text, missing), group['max_output_tokens'])
            complete_ids.update(s['id'] for s in missing)
        current = repository.read(settings, task_id)
        context = rules.report_context(current['details'], task['rules'], source_count=task['source_count'],
                                       product_context=plan['context'] if plan else None)
        previous = next((c for c in reversed(current['calls']) if c['kind'] == 'report' and c['status'] == 'succeeded'), None)
        if previous:
            narrative = rules.validate_report(previous['response']['text'], context)
        else:
            narrative = request('report', 0, task['rules']['report'] + json.dumps(context, ensure_ascii=False), [],
                                lambda text: rules.validate_report(text, context),
                                plan['report_output_tokens'] if plan else task['parameters']['max_output_tokens'])
    except ValueError as error:
        issues.append(str(error))
    build_report(settings, task_id, narrative=narrative, issues=issues)
    status = 'succeeded' if not issues else 'partial' if complete_ids else 'failed'
    message = '分析完成，结果尚未人工复核。' if not issues else (
        '逐条分析完成，总报告未完成；' if len(complete_ids) == task['source_count'] else
        '本轮停止，已保存部分结果；' if complete_ids else '本轮停止，暂无通过校验的评价；') + issues[0]
    repository.update_task(settings, task_id, status=status, active_key=None, owner=None, message=message)
    return status
