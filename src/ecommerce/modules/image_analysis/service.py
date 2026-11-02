"""图片分析预览、明确付费与独立阅读入口。"""
from ecommerce.infrastructure.ai.config import load_config, api_key
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.tasks.ownership import owner_alive
from ecommerce.modules.review_analysis.service import product_id, identifier, launch
from . import store,planner


def dashboard(settings,product):
    """读取独立图片历史及批次候选，不转换或发送图片。"""
    product_id(product); value=store.history(settings,product)
    value['ready']=any(value['choices'].values())
    return value


def preview(settings,product,main_batch=None,detail_batch=None,model=None,output_tokens=None,allow_partial=False):
    """本地预检完整图片范围，问题明确展示，只有确认部分范围才规划。"""
    product_id(product)
    with repository.connection(settings) as (conn,tables):
        incoming,_,choices=store.inputs(settings,conn,tables,product,main_batch,detail_batch,allow_partial)
    config=load_config(settings.root); model=model or 'deepseek-flash'; rules=planner.snapshot(); plan=None; blocked=''
    try:
        if incoming['scope']['issues'] and not allow_partial: raise ValueError('有缺失或不可用图片，请核对后明确允许仅分析可用范围')
        plan=planner.plan(settings.root,config,model,incoming,rules,output_tokens)
    except ValueError as error: blocked=str(error)
    selected={v['module']:v['id'] for v in incoming['batches']}
    return dict(input=incoming,choices=choices,plan=plan,rules=rules,blocked=blocked,model=model,
        models=tuple(v for v in config.models if v=='deepseek-flash'),provider=config.provider,key_configured=bool(api_key(config)),
        main_batch=selected.get('main_images',0),detail_batch=selected.get('detail_images',0),allow_partial=allow_partial,
        output_tokens=output_tokens or config.report_output_tokens,input_hash=fingerprint(incoming),
        plan_hash=fingerprint(dict(plan=plan,rules=rules)))


def start(settings,product,*,main_batch,detail_batch,model,input_hash,plan_hash,allow_paid=False,force=False,output_tokens=None,allow_partial=False):
    """重核预览后创建固定计划，未确认费用不建任务。"""
    if allow_paid is not True: raise ValueError('请明确允许发送所选图片并承担AI调用费用')
    if type(force) is not bool: raise ValueError('重新生成选项无效')
    data=preview(settings,product,main_batch,detail_batch,model,output_tokens,allow_partial)
    if data['input_hash']!=input_hash or data['plan_hash']!=plan_hash: raise ValueError('图片或计划已变化，请重新预览，未发送')
    if not data['plan']: raise ValueError(data['blocked'])
    if not data['key_configured']: raise ValueError('请先配置本机AI密钥')
    plan=data['plan']; config=load_config(settings.root)
    parameters=dict(plan=plan,budget_version=3,max_calls=plan['planned_calls'],max_tokens=plan['recommended_tokens'],
        max_output_tokens=plan['report_output_tokens'],budget_history=[],budget_mode='auto',
        config_file=settings.mysql.credentials_file or str(settings.root/'config/settings.toml'))
    selection=dict(main_batch=data['main_batch'],detail_batch=data['detail_batch'],allow_partial=allow_partial)
    row,created=store.create(settings,product,selection,input_hash,data['rules'],parameters,config.provider,model,force)
    if created: launch(settings,row['id'])
    return dict(id=row['id'],created=created,choice_required=not created and row['status']=='succeeded',
        message='已有相同图片与设置的成功报告，可直接查看；重新生成会另建记录并再次计费。' if not created and row['status']=='succeeded'
        else '已打开正在执行的相同分析，不重复发送。' if not created else '已创建独立图片分析任务。')


def read(settings,task_id):
    """只读图片类型任务及其固定来源，拒绝混读。"""
    data=repository.read(settings,identifier(task_id))
    if data['task']['analysis_type']!=store.TYPE or data['task']['rule_version']!=store.VERSION: raise LookupError('图片分析记录不存在')
    data['budget']=repository.budget_status(data['task']['parameters'],data['calls'])
    owner=data['task'].get('owner') or {}
    data['appears_interrupted']=data['task']['status'] in ('queued','running') and bool(owner) and owner_alive(owner.get('pid'),owner.get('started_at')) is False
    return data
