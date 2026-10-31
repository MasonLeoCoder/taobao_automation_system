"""问答分析业务入口，Web与其他入口可复用，预览不调用AI。"""
from ecommerce.infrastructure.ai.config import load_config,api_key
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.modules.review_analysis.service import product_id,integer,identifier,launch
from ecommerce.modules.question_analysis import planner,store,runtime


def preview(settings,product,batch_id,model=None,output_tokens=None):
    """整批脱敏和容量预检；参数：商品、批次、模型及输出量；返回：确认资料。"""
    product_id(product);integer(batch_id,1,2**63-1,'批次编号')
    with repository.connection(settings) as (conn,tables):
        identity,batch,items=store.sources(conn,tables,product,batch_id)
        context=repository.product_context(conn,tables,identity)
    config=load_config(settings.root);selected=model or config.model;rules=planner.snapshot()
    if selected not in config.models:raise ValueError('请选择已配置模型。')
    # 统计是程序计算的样本口径，随消息发送，不由AI猜测。
    rules=dict(rules,report=rules['report']+'\n本批程序统计：问题'+str(len(items))+'条；已采集回答'+str(sum(len(i['answers']) for i in items))+'条。仅代表本批样本。')
    plan=planner.plan(settings.root,config,selected,items,context,rules,output_tokens)
    return dict(product=identity,batch=batch,items=items,context=context,plan=plan,rules=rules,model=selected,models=config.models,
        key_configured=bool(api_key(config)),input_hash=fingerprint(items),plan_hash=fingerprint({'plan':plan,'rules':rules}),
        answer_count=sum(len(i['answers']) for i in items))


def start(settings,product,*,batch_id,model,input_hash,plan_hash,allow_paid=False,force=False,output_tokens=None):
    """核对确认的固定计划后创建并派发；参数：页面确认资料；返回：任务及明确选择状态。"""
    if allow_paid is not True:raise ValueError('请明确允许发送本批问答并承担AI调用费用。')
    if type(force) is not bool:raise ValueError('重新分析选项无效。')
    data=preview(settings,product,batch_id,model,output_tokens)
    if input_hash!=data['input_hash'] or plan_hash!=data['plan_hash']:raise ValueError('资料或计划已变化，请刷新预览后重新确认。')
    config=load_config(settings.root)
    if data['plan']['planned_calls'] and not data['key_configured']:raise ValueError('请先在本机AI设置中配置密钥。')
    plan=data['plan'];parameters={'plan':plan,'budget_version':3,'budget_mode':'auto','budget_history':[],
        'max_output_tokens':plan['report_output_tokens'],'max_calls':plan['planned_calls'],'max_tokens':plan['recommended_tokens'],
        'config_file':settings.mysql.credentials_file or str(settings.root/'config/settings.toml')}
    row,created=repository.create(settings,product,batch_id,data['rules'],parameters,config.provider,model,planner.VERSION,input_hash,force=force,analysis_type='questions')
    if created:launch(settings,row['id'])
    return {'id':row['id'],'choice_required':not created and not row['active_key'],
        'message':'已有相同资料与设置的记录，请查看已有结果，或明确选择从头重新分析（整批重新计费）。' if not created and not row['active_key'] else '已打开分析进度。'}


def read(settings,task_id):
    """仅读取问答分析，拒绝其他分析类型；参数：编号；返回：持久聚合。"""
    data=repository.read(settings,identifier(task_id))
    if data['task']['analysis_type']!='questions' or data['task']['rule_version']!=planner.VERSION:raise LookupError('问答分析记录不存在。')
    return data
