"""问答分析页面，写操作复用本机来源与令牌守卫。"""
import html
import json
import re
from flask import Blueprint,request,render_template,current_app,jsonify,Response,url_for
from ecommerce.web.routes.review_analysis import protect,payload
from ecommerce.modules.question_analysis import service,report,checks,runtime
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.time_values import iso_value
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
    service.product_id(product);batch=int(request.args.get('batch','0'));service.integer(batch,1,2**63-1,'批次编号')
    with repository.connection(settings()) as (conn,tables):service.store.sources(conn,tables,product,batch)
    rows=repository.history(settings(),product,batch,analysis_type='questions')
    for row in rows:row['created_at']=iso_value(row['created_at'])
    return render_template('question_analysis.html',mode='history',product=product,batch=batch,rows=rows,active='collections',shop_name=settings().shop_name)


@question_analysis.get('/analysis/questions/products/<product>/new')
def new(product):
    """读取发送预览；参数：商品/批次/模型/输出；返回：整页或弹窗内容。"""
    output=int(request.args['output_tokens']) if request.args.get('output_tokens') else None
    data=service.preview(settings(),product,int(request.args.get('batch','0')),request.args.get('model'),output)
    if request.args.get('dialog')=='1':return render_template('question_analysis_form.html',data=data,product=product)
    return render_template('question_analysis.html',mode='new',data=data,product=product,batch=data['batch']['id'],active='collections',shop_name=settings().shop_name)


@question_analysis.post('/api/analysis/questions/products/<product>')
def start(product):
    """创建固定计划；参数：JSON；返回：任务地址或已有记录选择。"""
    values=payload({'batch_id','model','input_hash','plan_hash','allow_paid','force','output_tokens'}, {'batch_id','model','input_hash','plan_hash'})
    value=service.start(settings(),product,**values);value['url']=url_for('.result',task_id=value['id'])
    return jsonify(value)


def render_report(content):
    """安全正文中按引用位置折叠；参数：持久报告；返回：安全HTML，不改原稿。"""
    from ecommerce.modules.question_analysis.view import evidence_html
    return evidence_html(content)


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
            body=render_template('question_analysis_export.html',data=data,content=content,rendered=render_report(content));mime='text/html'
        else:raise ValueError('下载格式无效。')
        return Response(body,content_type=mime+'; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="questions-{task_id}.{download}"'})
    from ecommerce.modules.question_analysis.view import reading
    return render_template('question_analysis.html',reading=reading(content) if content else None,mode='result',data=data,content=content,rendered=render_report(content) if content else '',
        product=data['product']['platform_product_id'],batch=data['sources'][0]['batch_id'],active='collections',shop_name=settings().shop_name)


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
