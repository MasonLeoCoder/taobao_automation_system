"""问答分析页面，写操作复用本机来源与令牌守卫。"""
import json
from flask import Blueprint,request,render_template,current_app,jsonify,Response,url_for
from ecommerce.web.routes.review_analysis import protect,payload
from ecommerce.modules.question_analysis import service,report,checks,runtime
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.ai.client import AIError
from ecommerce.shared.exceptions import TaskBusy

question_analysis=Blueprint('question_analysis',__name__)
question_analysis.before_request(protect)


def settings():
    """读取应用配置；参数：无；返回：配置。"""
    return current_app.config['SETTINGS']


@question_analysis.errorhandler(ValueError)
@question_analysis.errorhandler(LookupError)
@question_analysis.errorhandler(AIError)
@question_analysis.errorhandler(TaskBusy)
def invalid(error):
    """明确返回业务错误；参数：异常；返回：JSON或安全页面。"""
    if request.method=='POST':return jsonify(error=str(error)),400
    return render_template('review_analysis_error.html',message=str(error),active='collections',shop_name=settings().shop_name),400


@question_analysis.get('/analysis/questions/products/<product>')
def history(product):
    """读取指定批次历史；参数：平台商品；返回：历史页面，不联网。"""
    raw=request.args.get('batch')
    try:batch='all' if raw=='all' else int(raw) if raw else None
    except ValueError:raise ValueError('批次编号无效。') from None
    data=service.history_page(settings(),product,batch)
    return render_template('question_analysis_history.html',product=product,**data,active='collections',shop_name=settings().shop_name)


@question_analysis.get('/analysis/questions/products/<product>/new')
def new(product):
    """读取发送预览；参数：商品/批次/模型/输出；返回：整页或弹窗内容。"""
    output=int(request.args['output_tokens']) if request.args.get('output_tokens') else None
    data=service.preview(settings(),product,int(request.args.get('batch','0')),request.args.get('model'),output)
    if request.args.get('dialog')=='1':return render_template('question_analysis_form.html',data=data,product=product)
    return render_template('question_analysis.html',mode='new',data=data,product=product,batch=data['batch']['id'],active='collections',shop_name=settings().shop_name)


@question_analysis.get('/api/analysis/questions/products/<product>/summary')
def summary(product):
    """只读已应用批次的报告状态；参数：商品和批次；返回：成功报告与最近进度，不联网。"""
    import sqlalchemy as sa
    service.product_id(product);batch=int(request.args.get('batch','0'));service.integer(batch,1,2**63-1,'批次编号')
    with repository.connection(settings()) as (conn,tables):
        p,b=tables['products'],tables['collection_batches']
        exists=conn.scalar(sa.select(b.c.id).join(p,b.c.product_id==p.c.id).where(p.c.platform_product_id==product,p.c.platform=='taobao',b.c.id==batch,b.c.module=='questions',b.c.visibility=='published'))
        if not exists:raise LookupError('问答批次不属于该商品或尚未发布。')
    rows=repository.history(settings(),product,batch,analysis_type='questions',metadata_only=True)
    success=repository.latest_success(settings(),product,batch,analysis_type='questions',metadata_only=True)
    active=next((row for row in rows if row['active_key']),None);latest=rows[0] if rows else None
    message='本批尚未分析。'
    if active:
        from ecommerce.infrastructure.tasks.ownership import owner_alive
        owner=active.get('owner') or {}
        stopped=owner and owner_alive(owner.get('pid'),owner.get('started_at')) is False
        message='分析进程已停止，请查看任务记录。' if stopped else '本批正在分析。'
    elif success:message='本批已有成功报告。'+('最近一次分析未完成，此前报告保留。' if latest and latest['status']!='succeeded' else '')
    elif latest:message='上次分析未完成，可查看进度与已保存内容。'
    progress=active or (latest if latest and latest['status']!='succeeded' else None)
    return jsonify(message=message,result_url=url_for('.result',task_id=success['id']) if success else None,
        progress_url=url_for('.result',task_id=progress['id']) if progress else None)


@question_analysis.post('/api/analysis/questions/products/<product>')
def start(product):
    """创建固定计划；参数：JSON；返回：任务地址或已有记录选择。"""
    values=payload({'batch_id','model','input_hash','plan_hash','allow_paid','force','output_tokens'}, {'batch_id','model','input_hash','plan_hash'})
    value=service.start(settings(),product,**values);value['url']=url_for('.result',task_id=value['id'])
    return jsonify(value)


def render_report(content):
    """安全正文中按引用位置折叠；参数：持久报告；返回：安全HTML，不改原稿。"""
    from ecommerce.modules.question_analysis.view import evidence_html,paragraph_layout
    return paragraph_layout(evidence_html(content))


@question_analysis.get('/analysis/questions/tasks/<task_id>')
def result(task_id):
    """读取报告或历史版本；参数：任务编号；返回：阅读页面或下载。"""
    data=service.read(settings(),task_id)
    if request.args.get('version'):
        version=int(request.args['version']);data['report']=next((r for r in data['reports'] if r['version']==version),None)
        if data['report'] is None:raise LookupError('报告版本不存在。')
    content=data['report']['content'] if data['report'] else None
    download=request.args.get('download')
    if download:
        if content is None:raise ValueError('尚无已保存报告。')
        if download=='md':body=content['markdown'];mime='text/markdown'
        elif download=='json':body=json.dumps(data,ensure_ascii=False,default=str);mime='application/json'
        elif download=='html':
            # 阅读版内嵌现有公共样式，离线打开也保持逐项排列，不重复显示列表编号。
            styles=[]
            for filename in ('form_controls.css','question_analysis.css'):
                with current_app.open_resource('static/'+filename) as resource:styles.append(resource.read().decode('utf-8'))
            body=render_template('question_analysis_export.html',data=data,content=content,rendered=render_report(content),styles='\n'.join(styles));mime='text/html'
        else:raise ValueError('下载格式无效。')
        return Response(body,content_type=mime+'; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="questions-{task_id}.{download}"'})
    from ecommerce.modules.question_analysis.view import reading
    from ecommerce.web.report_navigation import origin
    return render_template('question_analysis.html',reading=reading(content) if content else None,mode='result',data=data,content=content,rendered=render_report(content) if content else '',
        product=data['product']['platform_product_id'],batch=data['sources'][0]['batch_id'],active='collections',shop_name=settings().shop_name,
        source_navigation=origin(settings(),data['product']['platform_product_id']))


@question_analysis.post('/api/analysis/questions/tasks/<task_id>/check')
def recheck(task_id):
    """明确本地重查并追加报告版本；参数：任务及原报告；返回：零AI调用摘要。"""
    values=payload({'report_id'},{'report_id'});data=service.read(settings(),task_id)
    from ecommerce.infrastructure.tasks.lock import shop_lock
    from ecommerce.infrastructure.tasks.ownership import owner_alive
    with shop_lock(settings().workspace/'storage/analysis_dispatch'/task_id):
        owner=data['task'].get('owner') or {}
        if owner and owner_alive(owner.get('pid'),owner.get('started_at')) is not False:raise ValueError('分析进程仍存活或身份未知，暂不能本地重查。')
        return jsonify(runtime.recheck(settings(),data,values['report_id']))
