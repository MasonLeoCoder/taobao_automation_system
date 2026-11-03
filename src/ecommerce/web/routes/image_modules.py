"""主图/详情图共用的薄Web入口，URL限定模块，POST不接受跨模块来源。"""
from flask import Blueprint,current_app,request,render_template,jsonify,url_for,Response
from ecommerce.modules.image_analysis import independent,report,runtime
from ecommerce.modules.image_analysis.kinds import kind
from ecommerce.web.routes.review_analysis import protect,payload
from ecommerce.web.routes.image_analysis import invalid,result_view
from ecommerce.infrastructure.images import vision
from ecommerce.shared.exceptions import TaskBusy

image_modules=Blueprint('image_modules',__name__)
image_modules.before_request(protect)
for error in (ValueError,LookupError,TaskBusy): image_modules.register_error_handler(error,invalid)


def settings():
    """读取当前本机应用配置。"""
    return current_app.config['SETTINGS']


def batch_arg():
    """读取可选正数批次，0仅历史表示全部，不用于分析。"""
    value=request.args.get('batch')
    if value in (None,'','0'): return None
    result=int(value)
    if result<1: raise ValueError('批次编号无效')
    return result


@image_modules.get('/analysis/images/<module>/products/<product>')
def history(module,product):
    """读取对应类型的历史及明确批次范围。"""
    data=independent.history(settings(),product,module,batch_arg())
    return render_template('image_module_history.html',data=data,product=product,active='collections',shop_name=settings().shop_name)


@image_modules.get('/analysis/images/<module>/products/<product>/new')
def new(module,product):
    """仅预检，不创建任务；范围由URL模块和单一batch共同限定。"""
    if set(request.args)-{'dialog','batch','model','output_tokens','allow_partial','origin'}: raise ValueError('独立图片预览参数无效')
    if request.args.get('batch')=='0': raise ValueError('分析必须选择具体图片批次')
    if request.args.get('allow_partial') not in (None,'0','1'): raise ValueError('部分范围确认无效')
    data=independent.preview(settings(),product,module,batch_arg(),request.args.get('model'),
        int(request.args['output_tokens']) if request.args.get('output_tokens') else None,request.args.get('allow_partial')=='1')
    return render_template('image_module_form.html' if request.args.get('dialog')=='1' else 'image_module_new.html',
        data=data,product=product,active='collections',shop_name=settings().shop_name)


@image_modules.post('/api/analysis/images/<module>/products/<product>')
def start(module,product):
    """费用确认后创建独立任务，不接受另一模块批次或客户端规则。"""
    kind(module)
    values=payload({'batch_id','model','input_hash','plan_hash','allow_paid','force','output_tokens','allow_partial'},
        {'batch_id','model','input_hash','plan_hash'})
    value=independent.start(settings(),product,module,**values)
    value['url']=url_for('.result',module=module,task_id=value['id'])
    return jsonify(value)


@image_modules.get('/analysis/images/<module>/tasks/<task_id>')
def result(module,task_id):
    """独立类型及版本严格读取，复用公共阅读和导出。"""
    return result_view(independent.read(settings(),task_id,module))


@image_modules.get('/analysis/images/<module>/tasks/<task_id>/frames/<sha>')
def frame(module,task_id,sha):
    """只返回本独立任务中固定的处理图片。"""
    data=independent.read(settings(),task_id,module)
    selected=next((f for s in data['task']['parameters']['plan']['input']['images'] for f in s['frames'] if f['sha256']==sha),None)
    if not selected: raise LookupError('切片不属于当前模块任务')
    return Response(vision.cached(settings(),selected),content_type='image/png',headers={'Cache-Control':'private, max-age=3600'})


@image_modules.post('/api/analysis/images/<module>/tasks/<task_id>/check')
def recheck(module,task_id):
    """明确本地检查，旧报告和调用保留，零AI费用。"""
    from ecommerce.infrastructure.tasks.lock import shop_lock
    values=payload({'report_id'},{'report_id'})
    with shop_lock(settings().workspace/'storage/analysis_workers'/independent.identifier(task_id)):
        return jsonify(runtime.recheck(settings(),independent.read(settings(),task_id,module),values['report_id']))
