"""综合选品业务入口，预览只读、付费需确认、来源版本不可漂移。"""
from ecommerce.infrastructure.ai.config import load_config, api_key
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.tasks.ownership import owner_alive
from ecommerce.modules.review_analysis.service import product_id, identifier, launch
from ecommerce.modules.composite_analysis import store, planner


def dashboard(settings, product):
    """读取候选来源、历史和新资料提示；参数：商品；返回：纯只读页面模型。"""
    product_id(product)
    with repository.connection(settings) as (conn, tables):
        item = store.identity(conn, tables, product)
        choices = {kind: store.choices(conn, tables, item['id'], kind) for kind in ('reviews', 'questions')}
    value = store.history(settings, product)
    for row in value['tasks']:
        owner = row.get('owner') or {}
        row['appears_interrupted'] = row['status'] in ('queued', 'running') and bool(owner) and owner_alive(owner.get('pid'), owner.get('started_at')) is False
    value.update(choices=choices, ready=all(choices.values()), product_title=item.get('display_title'))
    if value['latest_result']:
        value['has_new_sources'] = any(choices[s['kind']] and choices[s['kind']][0]['id'] != s['report_id'] for s in value['latest_sources'])
    return value


def preview(settings, product, review_report=None, question_report=None, model=None, output_tokens=None):
    """生成完整实际发送预览；参数：商品、明确报告版本及设置；返回：固定计划，不联网。"""
    product_id(product)
    with repository.connection(settings) as (conn, tables):
        item = store.identity(conn, tables, product)
        choices = {kind: store.choices(conn, tables, item['id'], kind) for kind in ('reviews', 'questions')}
        selected = []
        for kind, value in (('reviews', review_report), ('questions', question_report)):
            if value is None:
                if not choices[kind]: raise ValueError('请先生成可读的评价报告和问大家报告，再进行综合分析。')
                value = choices[kind][0]['id']
            identifier(value); selected.append(value)
        incoming, _, item = store.inputs(conn, tables, product, *selected)
    # 明确请求的历史版本即便不在最近100项中，也必须保留选中项，不能悄悄切换。
    for source in incoming['reports']:
        if not any(v['id'] == source['report_id'] for v in choices[source['kind']]):
            choices[source['kind']].insert(0, dict(id=source['report_id'], version=source['version'], batch_id=source['batch_ids'][0],
                source_count=source['source_count'], created_at=source['created_at'], pending_count=source['pending_citations']))
    config = load_config(settings.root); selected_model = model or config.model; rules = planner.snapshot()
    plan = planner.plan(settings.root, config, selected_model, incoming, rules, output_tokens)
    return {'input': incoming, 'plan': plan, 'rules': rules, 'choices': choices, 'model': selected_model, 'models': config.models,
        'provider': config.provider, 'key_configured': bool(api_key(config)), 'review_report': selected[0], 'question_report': selected[1],
        'input_hash': fingerprint(incoming), 'plan_hash': fingerprint({'plan': plan, 'rules': rules})}


def start(settings, product, *, review_report, question_report, model, input_hash, plan_hash, allow_paid=False, force=False, output_tokens=None):
    """确认后原子创建并派发；参数：页面固定计划及授权；返回：新任务或已有结果选择。"""
    if allow_paid is not True: raise ValueError('请明确允许发送所选报告及相关原文，并承担AI调用费用。')
    if type(force) is not bool: raise ValueError('重新生成选项无效。')
    data = preview(settings, product, review_report, question_report, model, output_tokens)
    if data['input_hash'] != input_hash or data['plan_hash'] != plan_hash:
        raise ValueError('来源或发送计划已变化，请重新预览并确认，未发送。')
    if not data['key_configured']: raise ValueError('请先在本机AI设置中配置密钥。')
    plan = data['plan']; config = load_config(settings.root)
    parameters = {'plan': plan, 'budget_version': 3, 'max_calls': plan['planned_calls'], 'max_tokens': plan['recommended_tokens'],
        'max_output_tokens': plan['report_output_tokens'], 'budget_history': [], 'budget_mode': 'auto',
        'config_file': settings.mysql.credentials_file or str(settings.root/'config/settings.toml')}
    row, created = store.create(settings, product, (review_report, question_report), input_hash, data['rules'], parameters, config.provider, model, force)
    if created: launch(settings, row['id'])
    return {'id': row['id'], 'created': created, 'choice_required': not created and row['status'] == 'succeeded',
        'message': '已有相同来源、提示和模型设置的成功报告，可直接查看；重新生成会另建记录并再次计费。' if not created and row['status'] == 'succeeded'
        else '已打开正在执行的相同分析，不重复发送。' if not created else '已创建独立综合分析任务。'}


def read(settings, task_id):
    """只读独立综合任务；参数：编号；返回：聚合，拒绝混读其他类型。"""
    data = repository.read(settings, identifier(task_id))
    if data['task']['analysis_type'] != store.TYPE or data['task']['rule_version'] != store.VERSION:
        raise LookupError('综合分析记录不存在。')
    data['budget'] = repository.budget_status(data['task']['parameters'], data['calls'])
    owner = data['task'].get('owner') or {}
    data['appears_interrupted'] = data['task']['status'] in ('queued', 'running') and bool(owner) and owner_alive(owner.get('pid'), owner.get('started_at')) is False
    return data
