"""综合选品页面与明确付费操作；读取与下载不调用AI。"""
import json
from flask import Blueprint, request, render_template, current_app, jsonify, Response, url_for, redirect
from ecommerce.web.routes.review_analysis import protect, payload
from ecommerce.modules.composite_analysis import service, report, runtime
from ecommerce.shared.exceptions import TaskBusy
from ecommerce.web import report_navigation

composite_analysis = Blueprint('composite_analysis', __name__)
composite_analysis.before_request(protect)


def settings():
    """读取配置；参数：无；返回：应用配置。"""
    return current_app.config['SETTINGS']


@composite_analysis.errorhandler(ValueError)
@composite_analysis.errorhandler(LookupError)
@composite_analysis.errorhandler(TaskBusy)
def invalid(error):
    """错误明确展示，不用空报告伪装；参数：错误；返回：安全页面或JSON。"""
    if request.method == 'POST' or request.path.startswith('/api/'):
        return jsonify(error=str(error)), 400
    if request.args.get('dialog') == '1':
        return render_template('review_analysis_dialog_error.html', message=str(error)), 400
    return render_template('review_analysis_error.html', message=str(error), active='collections', shop_name=settings().shop_name), 400


@composite_analysis.get('/analysis/composite/products/<product>')
def history(product):
    """统一返回综合分析页签；参数：商品；返回：站内跳转。"""
    service.product_id(product)
    return redirect(url_for('pages.collection_product', product_id=product, module='composite'))


@composite_analysis.get('/analysis/composite/products/<product>/new')
def new(product):
    """读取预览和候选，不创建任务；参数：所选版本和模型；返回：弹窗或整页。"""
    raw = request.args.get('output_tokens')
    output = int(raw) if raw else None
    data = service.preview(settings(), product, request.args.get('review_report'), request.args.get('question_report'), request.args.get('model'), output)
    default_return = url_for('.new', product=product, review_report=data['review_report'], question_report=data['question_report'],
        model=data['model'], output_tokens=data['plan']['report_output_tokens'])
    candidate = report_navigation.target(settings(), product, request.args['origin']) if request.args.get('origin') else None
    source_return = candidate[0] if candidate and candidate[1] != '综合分析设置' else default_return
    return render_template('composite_analysis_form.html' if request.args.get('dialog') == '1' else 'composite_analysis_new.html',
        data=data, product=product, source_return=source_return, active='collections', shop_name=settings().shop_name)


@composite_analysis.get('/analysis/composite/products/<product>/return')
def source_return(product):
    """点击返回时重新核验原页面，失效回综合页，仅读取、不恢复任务。"""
    service.product_id(product)
    destination, _, valid = report_navigation.target(settings(), product, request.args.get('target'))
    if valid and '/new' in destination:
        # 来源在离开期间可能被移除或模型设置失效，避免跳到无法渲染的设置页。
        from urllib.parse import urlsplit, parse_qs
        values = {k: v[0] for k, v in parse_qs(urlsplit(destination).query).items()}
        try:
            service.preview(settings(), product, values.get('review_report'), values.get('question_report'), values.get('model'),
                int(values['output_tokens']) if values.get('output_tokens') else None)
        except (ValueError, LookupError):
            destination = url_for('pages.collection_product', product_id=product, module='composite'); valid = False
    from urllib.parse import urlencode
    import re
    state = request.args.get('state', '')
    values = {'_report_back': state} if valid and re.fullmatch(r'[a-f0-9]{32}', state) else {}
    if not valid: values['_report_return_notice'] = '1'
    if values: destination += ('&' if '?' in destination else '?')+urlencode(values)
    return redirect(destination)


@composite_analysis.post('/api/analysis/composite/products/<product>')
def start(product):
    """创建确认的固定计划；参数：JSON；返回：任务地址或已有成功报告选择。"""
    values = payload({'review_report','question_report','model','input_hash','plan_hash','allow_paid','force','output_tokens'},
                     {'review_report','question_report','model','input_hash','plan_hash'})
    value = service.start(settings(), product, **values); value['url'] = url_for('.result', task_id=value['id'])
    return jsonify(value)


@composite_analysis.get('/analysis/composite/tasks/<task_id>')
def result(task_id):
    """读取固定来源及所选报告版本；参数：编号和可选下载；返回：安全报告。"""
    data = service.read(settings(), task_id)
    if request.args.get('version'):
        version = int(request.args['version']); data['report'] = next((r for r in data['reports'] if r['version'] == version), None)
        if not data['report']: raise LookupError('报告版本不存在。')
    content = data['report']['content'] if data['report'] else None
    product = data['product']['platform_product_id']; download = request.args.get('download')
    if download:
        if not content: raise ValueError('尚无已保存报告。')
        if download == 'md': body = content['markdown']; mime = 'text/markdown'
        elif download == 'json': body = json.dumps(data, ensure_ascii=False, default=str); mime = 'application/json'
        elif download == 'html':
            styles = []
            for filename in ('form_controls.css', 'composite_analysis.css'):
                with current_app.open_resource('static/'+filename) as file: styles.append(file.read().decode('utf-8'))
            body = render_template('composite_analysis_export.html', data=data, content=content, rendered=report.render(content), styles='\n'.join(styles)); mime = 'text/html'
        else: raise ValueError('下载格式无效。')
        return Response(body, content_type=mime+'; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="composite-{task_id}.{download}"'})
    return render_template('composite_analysis_result.html', data=data, content=content, reading=report.reading(content) if content else None,
        product=product, active='collections', shop_name=settings().shop_name)


@composite_analysis.post('/api/analysis/composite/tasks/<task_id>/check')
def recheck(task_id):
    """显式本地核对，运行中和过期页面拒绝；参数：报告身份；返回：零费用新版本。"""
    values = payload({'report_id'}, {'report_id'})
    from ecommerce.infrastructure.tasks.lock import shop_lock
    with shop_lock(settings().workspace/'storage/analysis_workers'/service.identifier(task_id)):
        return jsonify(runtime.recheck(settings(), service.read(settings(), task_id), values['report_id']))
