"""本机工具箱和主图导出入口；转换业务独立于Flask。"""
import secrets
from urllib.parse import urlsplit
from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from ecommerce.infrastructure.images.models import ImageConversionError, OUTPUT_FORMATS
from ecommerce.modules.image_tools.service import convert_uploads
from ecommerce.modules.image_tools.main_images import export_main_images

image_tools = Blueprint('image_tools', __name__)


@image_tools.before_request
def protect_request():
    """核对本机来源、令牌并单独放宽上传限额；参数：当前请求；返回：无或拒绝。"""
    if urlsplit(request.host_url).hostname not in {'localhost', '127.0.0.1', '::1'}:
        abort(403, description='工具箱仅供本机使用')
    if request.method == 'POST':
        if not secrets.compare_digest(request.headers.get('X-Collection-Token', ''), current_app.config['COLLECTION_TOKEN']):
            abort(403, description='页面已过期，请刷新后重试')
        origin = request.headers.get('Origin')
        if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
            abort(403, description='不接受其他网站发起的转换请求')
        if request.endpoint == 'image_tools.convert':
            request.max_content_length = 51 * 1024 * 1024
            request.max_form_memory_size = 256 * 1024
            request.max_form_parts = 25
        elif not request.is_json:
            abort(415, description='请使用下载格式选择窗口')


@image_tools.errorhandler(HTTPException)
def invalid_http(error):
    """返回可读接口错误；参数：HTTP异常；返回：JSON和原状态码。"""
    message = '上传内容超过限制，请一次选择最多20张、合计不超过50 MB的图片' if error.code == 413 else error.description
    return jsonify(error=message), error.code


@image_tools.errorhandler(ImageConversionError)
def conversion_error(error):
    """展示明确转换问题；参数：业务异常；返回：422 JSON。"""
    return jsonify(error=str(error)), 422


@image_tools.errorhandler(LookupError)
def missing(error):
    """拒绝错配来源；参数：未找到异常；返回：404 JSON。"""
    return jsonify(error=str(error)), 404


def download(artifact):
    """发送请求内临时产物；参数：下载结果；返回：关闭后释放文件的附件响应。"""
    try:
        response = send_file(artifact.stream, mimetype=artifact.mimetype, as_attachment=True,
                             download_name=artifact.filename, conditional=False)
        response.headers['X-Image-Count'] = str(artifact.count)
        response.call_on_close(artifact.stream.close)
        return response
    except BaseException:
        artifact.stream.close()
        raise


@image_tools.get('/tools')
def index():
    """展示当前已实现工具；参数：无；返回：工具箱首页。"""
    return render_template('tools/index.html', active='tools', shop_name=current_app.config['SETTINGS'].shop_name)


@image_tools.get('/tools/images')
def images():
    """展示独立图片工具；参数：无；返回：格式转换页面。"""
    return render_template('tools/image_converter.html', active='tools', shop_name=current_app.config['SETTINGS'].shop_name)


@image_tools.post('/api/tools/images/convert')
def convert():
    """读取多文件表单并返回下载；参数：files及format；返回：单图或ZIP。"""
    if request.mimetype != 'multipart/form-data':
        abort(415, description='请选择本地图片上传')
    if set(request.files) != {'files'} or set(request.form) != {'format'} or len(request.form.getlist('format')) != 1:
        abort(400, description='上传内容无效，请重新选择图片')
    files = [(item.filename or '图片', item.stream) for item in request.files.getlist('files')]
    return download(convert_uploads(files, request.form['format'],
                    temp_dir=current_app.config['SETTINGS'].workspace/'output/temp/image_tools'))


@image_tools.post('/api/tools/images/products/<product_id>/batches/<int:batch_id>/download')
def main_images(product_id, batch_id):
    """仅导出固定商品批次的主图；参数：格式和可选媒体编号；返回：图片或ZIP。"""
    import re
    if current_app.config['SETTINGS'].collection_backend != 'mysql':
        abort(409, description='采集数据库尚未启用')
    body = request.get_json()
    if not re.fullmatch(r'[1-9][0-9]{0,24}', product_id) or batch_id < 1:
        abort(400, description='商品或批次编号无效')
    if not isinstance(body, dict) or set(body) - {'format', 'media_id'} or not isinstance(body.get('format'), str) or body['format'] not in OUTPUT_FORMATS:
        abort(400, description='下载格式无效')
    media_id = body.get('media_id')
    if 'media_id' in body and (type(media_id) is not int or media_id < 1):
        abort(400, description='图片编号无效')
    return download(export_main_images(current_app.config['SETTINGS'], product_id, batch_id, body['format'], media_id=media_id))
