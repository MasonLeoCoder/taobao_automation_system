"""交叉分析只读预览及明确付费启动，旧综合类型不进入新功能。"""
from ecommerce.infrastructure.ai.config import load_config, api_key
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.tasks.ownership import owner_alive
from ecommerce.modules.review_analysis.service import product_id, identifier, launch
from . import store, planner
from .kinds import KINDS, TYPE, VERSION, ordered, combination, focus


def dashboard(settings, product):
    """读取四类候选与缺失状态及新历史，不创建任何分析任务。"""
    product_id(product)
    with repository.connection(settings) as (conn,tables):
        item = store.identity(conn,tables,product)
        choices = {kind:store.choices(conn,tables,item['id'],kind) for kind in KINDS}
        availability = {kind:store.availability(conn,tables,item['id'],kind,choices[kind]) for kind in KINDS}
    data = store.history(settings,product)
    for row in data['tasks']:
        owner = row.get('owner') or {}
        row['appears_interrupted'] = row['status'] in ('queued','running') and bool(owner) and owner_alive(owner.get('pid'),owner.get('started_at')) is False
    data.update(choices=choices,availability=availability,kinds=KINDS,ready=sum(bool(v) for v in choices.values()) >= 2,
        product_title=item.get('display_title'),defaults={k:v[0]['id'] for k,v in choices.items() if v})
    data['has_new_sources'] = bool(data['latest_result']) and any(choices[s['kind']] and
        choices[s['kind']][0]['id'] != s['report_id'] for s in data['latest_sources'])
    return data


def preview(settings, product, selection=None, model=None, output_tokens=None):
    """生成固定2～4份报告预览；不足两类返回选择界面，不规划或联网。"""
    data = dashboard(settings,product)
    selection = data['defaults'] if selection is None else ordered(selection,minimum=0)
    # 已明确选择的历史版本即使不在候选前100项内也必须保留，不能自动切换。
    with repository.connection(settings) as (conn,tables):
        item = store.identity(conn,tables,product)
        for kind,report_id in selection.items():
            source,_ = store.load_source(conn,tables,item['id'],report_id,kind)
            if not any(v['id'] == report_id for v in data['choices'][kind]):
                data['choices'][kind].append(dict(id=report_id,analysis_id=source['analysis_id'],version=source['version'],
                    created_at=source['created_at'],batch_id=source['batch_ids'][0],captured_at=source['scope'][0].get('captured_at'),
                    source_count=source['source_count'],pending_count=source['pending_citations'],complete=source['generation'].get('complete')))
        incoming = store.inputs(conn,tables,product,selection)[0] if len(selection) >= 2 else None
    config = load_config(settings.root); selected_model = model or config.model
    rules = planner.snapshot(selection) if incoming else None
    plan = planner.plan(settings.root,config,selected_model,incoming,rules,output_tokens) if incoming else None
    data.update(input=incoming,plan=plan,rules=rules,selection=selection,combination=combination(selection),focus=focus(selection),
        model=selected_model,models=config.models,provider=config.provider,key_configured=bool(api_key(config)),
        input_hash=fingerprint(incoming) if incoming else '',plan_hash=fingerprint(dict(plan=plan,rules=rules)) if plan else '')
    return data


def start(settings, product, *, selection, model, input_hash, plan_hash, allow_paid=False, force=False, output_tokens=None):
    """明确费用后重核并创建；活动复用，成功由用户选择，失败不复用。"""
    selection = ordered(selection)
    if allow_paid is not True: raise ValueError('请明确允许发送所选报告及相关依据，并承担AI调用费用。')
    if type(force) is not bool: raise ValueError('重新生成选项无效。')
    data = preview(settings,product,selection,model,output_tokens)
    if data['input_hash'] != input_hash or data['plan_hash'] != plan_hash:
        raise ValueError('来源或计划已变化，请重新预览并确认，未发送。')
    if not data['key_configured']: raise ValueError('请先配置本机AI密钥。')
    plan = data['plan']; config = load_config(settings.root)
    parameters = dict(plan=plan,budget_version=3,max_calls=plan['planned_calls'],max_tokens=plan['recommended_tokens'],
        max_output_tokens=plan['report_output_tokens'],budget_history=[],budget_mode='auto',
        config_file=settings.mysql.credentials_file or str(settings.root/'config/settings.toml'))
    row,created = store.create(settings,product,selection,input_hash,data['rules'],parameters,config.provider,model,force)
    if created: launch(settings,row['id'])
    return dict(id=row['id'],created=created,choice_required=not created and row['status'] == 'succeeded',
        message='已有相同来源版本与设置的成功报告，可查看；重新生成会再次计费。' if not created and row['status'] == 'succeeded'
        else '已打开正在执行的相同交叉分析。' if not created else '已创建交叉分析任务。')


def read(settings, task_id):
    """只读新交叉任务，严格区分旧综合与图片联合报告。"""
    data = repository.read(settings,identifier(task_id)); task = data['task']
    if task['analysis_type'] != TYPE or task['rule_version'] != VERSION: raise LookupError('交叉分析记录不存在。')
    incoming = task['parameters']['plan']['input']; selection = {r['kind']:r['report_id'] for r in incoming['reports']}
    if list(ordered(selection)) != incoming['categories'] or len(selection) != len(incoming['reports']):
        raise ValueError('交叉报告固定类别不一致。')
    data['budget'] = repository.budget_status(task['parameters'],data['calls'])
    owner = task.get('owner') or {}
    data['appears_interrupted'] = task['status'] in ('queued','running') and bool(owner) and owner_alive(owner.get('pid'),owner.get('started_at')) is False
    return data
