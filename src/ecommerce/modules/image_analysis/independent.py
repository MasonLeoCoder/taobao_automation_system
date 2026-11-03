"""主图和详情图共用的独立分析业务入口，来源与历史严格隔离。"""
from ecommerce.infrastructure.ai.config import load_config, api_key
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.tasks.ownership import owner_alive
from ecommerce.modules.review_analysis.service import product_id, identifier, launch
from . import store, planner
from .kinds import kind
import sqlalchemy as sa


def preview(settings,product,module,batch_id=None,model=None,output_tokens=None,allow_partial=False):
    """预检单一模块整批图片；参数：商品、模块、批次和设置；返回：不付费计划。"""
    product_id(product); definition=kind(module)
    if batch_id is not None and (type(batch_id) is not int or batch_id<1): raise ValueError('请选择有效的图片批次')
    selection={'main_batch' if module=='main_images' else 'detail_batch':batch_id,'module':module,'allow_partial':allow_partial}
    with repository.connection(settings) as (conn,tables):
        incoming,_,choices=store.inputs(settings,conn,tables,product,**selection)
    config=load_config(settings.root); model=model or 'deepseek-flash'; rules=planner.snapshot(module); plan=None; blocked=''
    try:
        if incoming['scope']['issues'] and not allow_partial: raise ValueError('有缺失或不可用图片，请核对后明确允许仅分析可用范围')
        plan=planner.plan(settings.root,config,model,incoming,rules,output_tokens)
    except ValueError as error: blocked=str(error)
    return dict(kind=definition,module=module,label=definition['label'],input=incoming,choices=choices[module],plan=plan,rules=rules,
        blocked=blocked,model=model,models=tuple(v for v in config.models if v=='deepseek-flash'),key_configured=bool(api_key(config)),
        provider=config.provider,batch_id=incoming['batches'][0]['id'] if incoming['batches'] else 0,allow_partial=allow_partial,
        output_tokens=output_tokens or config.report_output_tokens,input_hash=fingerprint(incoming),plan_hash=fingerprint(dict(plan=plan,rules=rules)))


def start(settings,product,module,*,batch_id,model,input_hash,plan_hash,allow_paid=False,force=False,output_tokens=None,allow_partial=False):
    """明确费用后重核并派发单一模块，不重复提交活动任务。"""
    kind(module)
    if allow_paid is not True: raise ValueError('请明确允许发送本批图片并承担AI调用费用')
    if type(batch_id) is not int or batch_id<1: raise ValueError('开始分析必须明确指定具体图片批次')
    if type(force) is not bool: raise ValueError('重新生成选项无效')
    data=preview(settings,product,module,batch_id,model,output_tokens,allow_partial)
    if data['input_hash']!=input_hash or data['plan_hash']!=plan_hash: raise ValueError('图片、提示或计划已变化，请重新预览，未发送')
    if not data['plan']: raise ValueError(data['blocked'])
    if not data['key_configured']: raise ValueError('请先配置本机AI密钥')
    plan=data['plan']; config=load_config(settings.root)
    parameters=dict(plan=plan,budget_version=3,max_calls=plan['planned_calls'],max_tokens=plan['recommended_tokens'],
        max_output_tokens=plan['report_output_tokens'],budget_history=[],budget_mode='auto',
        config_file=settings.mysql.credentials_file or str(settings.root/'config/settings.toml'))
    selection=dict(module=module,allow_partial=allow_partial,**{'main_batch' if module=='main_images' else 'detail_batch':data['batch_id']})
    row,created=store.create(settings,product,selection,input_hash,data['rules'],parameters,config.provider,model,force)
    if created: launch(settings,row['id'])
    return dict(id=row['id'],created=created,choice_required=not created and row['status']=='succeeded',
        message='已有相同来源、提示和设置的成功报告；可直接查看，重新生成会另建记录并再次计费。' if not created and row['status']=='succeeded'
        else '正在执行相同分析，已打开原任务，不重复发送。' if not created else '已创建'+data['label']+'独立分析任务。')


def read(settings,task_id,module=None):
    """读取新独立类型，拒绝联合旧任务、跨模块及冻结范围不一致。"""
    data=repository.read(settings,identifier(task_id)); task=data['task']; definition=kind(module or task['analysis_type'])
    module=definition['module']; incoming=task['parameters'].get('plan',{}).get('input',{})
    if task['analysis_type']!=module or task['rule_version']!=definition['version'] or task['rules'].get('module')!=module:
        raise LookupError('该模块的独立图片分析记录不存在')
    if incoming.get('module')!=module or len(incoming.get('batches',[]))!=1 or incoming['batches'][0]['module']!=module:
        raise ValueError('独立图片分析的固定来源不一致')
    if not data['sources'] or any(s['snapshot'].get('module')!=module or s['batch_id']!=incoming['batches'][0]['id'] for s in data['sources']):
        raise ValueError('独立图片分析的来源快照不一致')
    data['budget']=repository.budget_status(task['parameters'],data['calls']); data['kind']=definition
    owner=task.get('owner') or {}
    data['appears_interrupted']=task['status'] in ('queued','running') and bool(owner) and owner_alive(owner.get('pid'),owner.get('started_at')) is False
    return data


def history(settings,product,module,batch_id=None):
    """轻量查询独立历史与当前批次成功报告，不让正文参与排序。"""
    product_id(product); definition=kind(module)
    if batch_id is not None and (type(batch_id) is not int or batch_id<1): raise ValueError('图片历史批次无效')
    with repository.connection(settings) as (conn,tables):
        item=store.identity(conn,tables,product); t,s,r,b=(tables[n] for n in ('analysis_tasks','analysis_sources','analysis_reports','collection_batches'))
        choices=store.choices(conn,tables,item['id'])[module]
        if batch_id is not None and not conn.scalar(sa.select(b.c.id).where(b.c.id==batch_id,b.c.product_id==item['id'],b.c.module==module,b.c.visibility=='published')):
            raise LookupError('历史范围不属于该商品的已发布图片模块')
        batch=sa.select(sa.func.min(s.c.batch_id)).where(s.c.analysis_id==t.c.id).correlate(t).scalar_subquery()
        query=sa.select(*[t.c[k] for k in ('id','status','created_at','model','message','owner','source_count')],batch.label('batch_id'))
        query=query.where(t.c.product_id==item['id'],t.c.analysis_type==module,t.c.rule_version==definition['version'])
        if batch_id is not None: query=query.where(batch==batch_id)
        order=(t.c.created_at.desc(),t.c.id.desc())
        rows=[dict(v) for v in conn.execute(query.order_by(*order).limit(100)).mappings()]
        latest=conn.execute(query.where(t.c.status=='succeeded',sa.exists(sa.select(r.c.id).where(r.c.analysis_id==t.c.id)))
            .order_by(*order).limit(1)).mappings().one_or_none()
        for row in rows:
            owner=row.get('owner') or {}
            row['appears_interrupted']=row['status'] in ('queued','running') and bool(owner) and owner_alive(owner.get('pid'),owner.get('started_at')) is False
        return dict(kind=definition,choices=choices,tasks=rows,latest_result=dict(latest) if latest else None,batch_id=batch_id,product_title=item.get('display_title'))
