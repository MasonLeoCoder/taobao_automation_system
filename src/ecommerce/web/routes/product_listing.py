"""本机商品上架工作台；请求只导入资料、发起任务或递交人工反馈。"""
import secrets
import re
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from ecommerce.modules.product.web_materials import CATEGORY, GROUPS, LIMIT, MAX_FILES
from ecommerce.modules.product.web_listing import ListingConflict
from ecommerce.modules.product.listing_logs import MAX_CURSOR
from ecommerce.infrastructure.storage import publish_categories
from ecommerce.modules.product.listing_profiles import support
from ecommerce.modules.product.title_generation import TitleGeneration
from ecommerce.shared.exceptions import TaskBusy

product_listing = Blueprint('product_listing', __name__)


@product_listing.before_request
def protect_listing():
    """限制本机同源写入；参数：当前请求；返回：继续或拒绝。"""
    if urlsplit(request.host_url).hostname not in {'localhost', '127.0.0.1', '::1'}:
        abort(403, description='商品上架仅供本机使用')
    if request.method != 'POST':
        return
    if not secrets.compare_digest(request.headers.get('X-Collection-Token', ''), current_app.config['COLLECTION_TOKEN']):
        abort(403, description='页面令牌已失效，请刷新')
    if request.headers.get('Origin', request.host_url).rstrip('/') != request.host_url.rstrip('/'):
        abort(403, description='不接受其他网站发起的上架操作')
    if request.endpoint == 'product_listing.preflight':
        if request.mimetype != 'multipart/form-data':
            abort(415, description='请先选择并导入商品文件夹')
        request.max_content_length = LIMIT + 1024**2
        request.max_form_memory_size = 512 * 1024
        request.max_form_parts = MAX_FILES + 3
    elif not request.is_json:
        abort(415, description='请通过上架工作台操作')


@product_listing.errorhandler(HTTPException)
def http_error(error):
    """返回请求错误；参数：HTTP异常；返回：JSON及原状态。"""
    return jsonify(error='选择的文件超过 300 个或 512 MB' if error.code == 413 else error.description), error.code


@product_listing.errorhandler(ValueError)
@product_listing.errorhandler(TaskBusy)
def listing_error(error):
    """返回预检或状态错误；参数：异常；返回：提示及状态。"""
    return jsonify(error=str(error)), 409 if isinstance(error, (ListingConflict, TaskBusy)) else 422


@product_listing.errorhandler(LookupError)
def missing(error):
    """返回资源未找到；参数：异常；返回：404。"""
    return jsonify(error=str(error)), 404


def workbench():
    """读取本应用的上架服务；参数：无；返回：上架工作台。"""
    return current_app.extensions['product_listing']


@product_listing.get('/plans/listing')
def index():
    """展示现有菜单下的上架工作台；参数：无；返回：页面。"""
    settings = current_app.config['SETTINGS']
    use_catalog = settings.collection_backend == 'mysql'
    rows = publish_categories.catalog(settings)[0] if use_catalog else []
    choices = [dict(row, support=support(row)) for row in rows if row['is_visible'] and row['is_leaf']]
    return render_template('product_listing.html', active='plan:listing', shop_name=settings.shop_name,
        token=current_app.config['COLLECTION_TOKEN'], category=CATEGORY, groups=GROUPS,
        use_catalog=use_catalog, choices=choices, can_submit=settings.environment != 'verify',
        title_generation_enabled=use_catalog)


@product_listing.post('/api/listing/preflight')
def preflight():
    """导入选中的素材并自检；参数：文件分组和导航信息；返回：预检结果。"""
    if (set(request.files) - GROUPS.keys() or set(request.form) != {'keyword', 'category', 'brand'}
            or any(len(values) != 1 for _, values in request.form.lists())):
        abort(400, description='预检表单或素材分组不完整')
    try:
        data = workbench().preflight({key: [(item.filename, item.stream) for item in values]
                                      for key, values in request.files.lists()}, **request.form.to_dict())
    except OSError:
        current_app.logger.exception('上架自检资料写入失败')
        abort(503, description='暂时无法保存导入资料，请稍后重新点击「导入并自检」。')
    return jsonify(data), 422 if data['errors'] else 201


@product_listing.get('/api/listing/<identifier>')
def status(identifier):
    """读取本次预检或执行状态；参数：编号；返回：进度快照。"""
    return jsonify(workbench().snapshot(identifier))


@product_listing.get('/api/listing/<identifier>/logs')
def logs(identifier):
    """读取当前上架任务的安全日志摘要；参数：导入编号及单向游标；返回：日志页。"""
    if set(request.args) - {'after', 'before'} or len(request.args) > 1:
        abort(400, description='日志游标参数无效')
    cursors = {}
    for key, values in request.args.lists():
        if len(values) != 1 or not re.fullmatch(r'[0-9]{1,19}', values[0]):
            abort(400, description='日志游标参数无效')
        value = int(values[0])
        if value > MAX_CURSOR or (key == 'before' and value < 1):
            abort(400, description='日志游标参数无效')
        cursors[key] = value
    return jsonify(workbench().logs(identifier, **cursors))


@product_listing.post('/api/listing/<identifier>/start')
def start(identifier):
    """在后台启动一次已预检导入；参数：编号及仓库开关；返回：当前状态。"""
    data = request.get_json()
    if not isinstance(data, dict) or 'submit' not in data or set(data) - {'submit', 'titles', 'sku_titles'}:
        abort(400, description='请明确本次是否提交到仓库')
    if 'titles' in data and not isinstance(data['titles'], dict):
        abort(400, description='标题参数无效')
    if 'sku_titles' in data and not isinstance(data['sku_titles'], dict):
        abort(400, description='SKU 搜索标题参数无效')
    return jsonify(workbench().start(identifier, data['submit'], titles=data.get('titles'),
                                     sku_titles=data.get('sku_titles'))), 202


@product_listing.get('/api/listing/<identifier>/titles/source')
def title_source(identifier):
    return jsonify(TitleGeneration(workbench(), identifier).source(
        request.args.get('period', '7d'), request.args.get('batch') or None))


@product_listing.get('/api/listing/<identifier>/titles/result')
def title_result(identifier):
    return jsonify(TitleGeneration(workbench(), identifier).latest(request.args.get('attempt') or None))


@product_listing.post('/api/listing/<identifier>/titles/preview')
def title_preview(identifier):
    request.max_content_length = 16 * 1024
    return jsonify(TitleGeneration(workbench(), identifier).preview(request.get_json()))


@product_listing.post('/api/listing/<identifier>/titles/generate')
def title_generate(identifier):
    request.max_content_length = 16 * 1024
    data = request.get_json()
    if not isinstance(data, dict) or set(data) != {'input', 'key', 'allow_paid', 'retry'}:
        abort(400, description='请重新预览并确认本次请求')
    try:
        return jsonify(TitleGeneration(workbench(), identifier).generate(
            data['input'], data['key'], data['allow_paid'], data['retry']))
    except OSError:
        current_app.logger.exception('标题生成记录读写失败')
        abort(503, description='无法保存标题生成结果，请先查看已保存记录和费用。')


@product_listing.post('/api/listing/<identifier>/feedback')
def feedback(identifier):
    """接收针对当前人工提示的明确反馈；返回：当前任务状态。"""
    data = request.get_json()
    if not isinstance(data, dict) or set(data) != {'prompt_id', 'action'}:
        abort(400, description='人工反馈参数无效')
    return jsonify(workbench().feedback(identifier, data['prompt_id'], data['action']))


@product_listing.get('/api/listing/<identifier>/evidence/<version>')
def evidence(identifier, version):
    """展示本任务的当前证据；参数：任务和证据版本；返回：PNG。"""
    return send_file(workbench().evidence(identifier, version), mimetype='image/png')
