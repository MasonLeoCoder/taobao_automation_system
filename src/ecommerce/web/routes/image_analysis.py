"""主图与详情分析页面，GET预检不调用AI，付费仅明确POST启动。"""
import json
from flask import Blueprint,request,render_template,current_app,jsonify,Response,url_for,redirect
from ecommerce.web.routes.review_analysis import protect,payload
from ecommerce.web.routes.composite_analysis import invalid
from ecommerce.modules.image_analysis import service,report,runtime,store
from ecommerce.infrastructure.images import vision
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.shared.exceptions import TaskBusy
import sqlalchemy as sa

image_analysis=Blueprint('image_analysis',__name__)
image_analysis.before_request(protect)
for error in (ValueError,LookupError,TaskBusy): image_analysis.register_error_handler(error,invalid)


def settings():
    """返回应用配置。"""
    return current_app.config['SETTINGS']


def arguments():
    """读取有限预览参数，不接受任意文件地址。"""
    value={}
    for key in ('main_batch','detail_batch','output_tokens'):
        if request.args.get(key) is not None: value[key]=int(request.args[key])
    if request.args.get('model'): value['model']=request.args['model']
    if request.args.get('allow_partial') not in (None,'0','1'): raise ValueError('部分范围确认无效')
    value['allow_partial']=request.args.get('allow_partial')=='1'
    return value


@image_analysis.get('/analysis/images/products/<product>/new')
def new(product):
    """读取图片预检、完整发送描述及费用计划。"""
    service.product_id(product)
    return redirect(url_for('pages.collection_product',product_id=product,module='main_images'))


@image_analysis.get('/analysis/images/products/<product>/preview')
def preview_image(product):
    """只返回已校验归属及摘要的处理切片，不支持任意路径。"""
    service.product_id(product); batch=int(request.args['batch']); media=int(request.args['media']); piece=int(request.args['piece'])
    if piece<1: raise ValueError('片段编号无效')
    with repository.connection(settings()) as (conn,tables):
        item=store.identity(conn,tables,product); b,m,f=(tables[n] for n in ('collection_batches','media_references','file_assets'))
        row=conn.execute(sa.select(m.c.role,*f.c).join(b,b.c.id==m.c.batch_id).join(f,f.c.id==m.c.file_asset_id)
            .where(m.c.id==media,b.c.id==batch,b.c.product_id==item['id'],b.c.visibility=='published',
                sa.or_(sa.and_(b.c.module=='main_images',m.c.role=='main_image'),sa.and_(b.c.module=='detail_images',m.c.role=='detail_image')))).mappings().one_or_none()
    if not row: raise LookupError('图片不属于所选发布批次')
    value=vision.prepare(settings(),dict(row)); frame=next((v for v in value['frames'] if v['piece']==piece and v['sha256']==request.args.get('sha')),None)
    if not frame: raise ValueError('图片已变化，请重新预览')
    return Response(vision.cached(settings(),frame),content_type='image/png',headers={'Cache-Control':'private, max-age=3600'})


@image_analysis.post('/api/analysis/images/products/<product>')
def start(product):
    """明确费用确认后创建；不自动重复POST。"""
    raise ValueError('联合图片分析入口已停用，请进入主图或详情图片模块分别分析')


@image_analysis.get('/analysis/images/tasks/<task_id>')
def result(task_id):
    """固定版本阅读及三格式附件导出，零AI调用。"""
    return result_view(service.read(settings(),task_id))


def result_view(data):
    """共享独立与既有报告的安全阅读、固定版本及附件导出。"""
    task_id=data['task']['id']; module=data.get('kind',{}).get('module'); label=data.get('kind',{}).get('label')
    endpoint='image_modules.result' if module else 'image_analysis.result'
    def result_link(**values):
        """构造固定类型的版本及导出地址。"""
        return url_for(endpoint,task_id=task_id,**({'module':module} if module else {}),**values)
    def frame_link(sha):
        """构造本任务的图片副本地址。"""
        return url_for('image_modules.frame' if module else 'image_analysis.frame',task_id=task_id,sha=sha,**({'module':module} if module else {}))
    if request.args.get('version'):
        version=int(request.args['version']); data['report']=next((v for v in data['reports'] if v['version']==version),None)
        if not data['report']: raise LookupError('报告版本不存在')
    content=data['report']['content'] if data['report'] else None; product=data['product']['platform_product_id']
    download=request.args.get('download')
    if download:
        if not content: raise ValueError('尚无已保存报告')
        if download=='md': body=content['markdown']; mime='text/markdown'
        elif download=='json': body=json.dumps(data,ensure_ascii=False,default=str); mime='application/json'
        elif download=='html':
            styles=[]
            for filename in ('form_controls.css','composite_analysis.css','image_analysis.css'):
                with current_app.open_resource('static/'+filename) as file: styles.append(file.read().decode('utf-8'))
            body=render_template('image_analysis_export.html',data=data,content=content,rendered=report.render(content),styles='\n'.join(styles),report_title=label+'分析报告' if label else '主图与详情选品报告'); mime='text/html'
        else: raise ValueError('下载格式无效')
        return Response(body,content_type=mime+'; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="images-{task_id}.{download}"'})
    return render_template('image_analysis_result.html',data=data,content=content,reading=report.reading(content) if content else None,
        product=product,active='collections',shop_name=settings().shop_name,report_title=label+'分析报告' if label else '主图与详情选品报告',
        result_link=result_link,frame_link=frame_link,check_url=url_for('image_modules.recheck',module=module,task_id=task_id) if module else url_for('image_analysis.recheck',task_id=task_id),
        return_url=url_for('pages.collection_product',product_id=product,module=module,batch=data['task']['parameters']['plan']['input']['batches'][0]['id']) if module else url_for('pages.collection_product',product_id=product,module='composite'),
        return_label='返回'+label+'模块' if label else '返回综合分析')


@image_analysis.get('/analysis/images/tasks/<task_id>/frames/<sha>')
def frame(task_id,sha):
    """阅读保存的实际切片；摘要必须属于本任务，原件后续缺失不影响审计副本。"""
    data=service.read(settings(),task_id)
    target=next((f for s in data['task']['parameters']['plan']['input']['images'] for f in s['frames'] if f['sha256']==sha),None)
    if not target: raise LookupError('切片不属于本任务')
    return Response(vision.cached(settings(),target),content_type='image/png',headers={'Cache-Control':'private, max-age=3600'})


@image_analysis.post('/api/analysis/images/tasks/<task_id>/check')
def recheck(task_id):
    """显式本地检查图号，绝不调用视觉接口。"""
    from ecommerce.infrastructure.tasks.lock import shop_lock
    values=payload({'report_id'},{'report_id'})
    with shop_lock(settings().workspace/'storage/analysis_workers'/service.identifier(task_id)):
        return jsonify(runtime.recheck(settings(),service.read(settings(),task_id),values['report_id']))
