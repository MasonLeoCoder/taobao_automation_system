"""YouTube单视频接口：只传明确规格，不提供列表或批量动作。"""
from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from .video_tools import protect, invalid, missing, http_error

youtube_tools = Blueprint('youtube_tools', __name__)
youtube_tools.before_request(protect)
youtube_tools.register_error_handler(ValueError, invalid)
youtube_tools.register_error_handler(LookupError, missing)
youtube_tools.register_error_handler(HTTPException, http_error)


def service():
    """取得当前工具；参数：无；返回：单视频服务。"""
    return current_app.extensions['youtube_tools']


def body_only(keys):
    """验证请求字段；参数：允许键；返回：请求字典。"""
    body = request.get_json()
    if not isinstance(body, dict) or set(body) - keys:
        abort(400, description='请求参数无效；YouTube工具一次只下载当前视频')
    return body


@youtube_tools.errorhandler(OSError)
def file_error(error):
    """隐藏路径细节并说明磁盘失败；参数：异常；返回：JSON。"""
    return jsonify(error='文件读写失败，请检查磁盘空间、目录权限或文件占用'), 422


@youtube_tools.get('/tools/youtube')
def page():
    """显示单视频工具；参数：无；返回：页面。"""
    return render_template('tools/youtube.html', active='tools', shop_name=current_app.config['SETTINGS'].shop_name)


@youtube_tools.route('/api/tools/youtube', methods=['GET', 'POST'])
def jobs():
    """读取历史或解析单视频；参数：链接和登录选项；返回：记录编号或摘要。"""
    if request.method == 'GET':
        return jsonify(items=service().recent(), **service().disk())
    body = body_only({'input', 'interactive'})
    if not isinstance(body.get('input'), str) or type(body.get('interactive', False)) is not bool:
        abort(400, description='请输入一个YouTube视频链接')
    return jsonify(id=service().start(body['input'], body.get('interactive', False))), 202


@youtube_tools.post('/api/tools/youtube/settings')
def settings():
    """设置临时磁盘和独立网络；参数：允许设置；返回：有效配置。"""
    return jsonify(options=service().configure(body_only({'temp_dir', 'cache_warn_gib', 'idle_hours', 'proxy', 'js_runtime'})))


@youtube_tools.post('/api/tools/youtube/cleanup')
def cleanup():
    """清理过期终态文件；参数：明确动作；返回：磁盘占用。"""
    if request.get_json() != {'action': 'cleanup'}:
        abort(400, description='清理动作无效')
    return jsonify(cleaned=service().cleanup(), **service().disk())


@youtube_tools.route('/api/tools/youtube/<identifier>', methods=['GET', 'POST'])
def job(identifier):
    """处理单个固定资源动作；参数：记录和规格；返回：状态。"""
    if request.method == 'GET':
        return jsonify(service().snapshot(identifier))
    body = body_only({'action', 'kind', 'video_id', 'audio_id', 'output_format'})
    if any(not isinstance(v, str) or len(v) > 100 for v in body.values()):
        abort(400, description='动作或规格格式无效')
    action = body.get('action')
    if action == 'prepare' and 'kind' in body:
        key = service().prepare(identifier, **{k: v for k, v in body.items() if k != 'action'})
        return jsonify(key=key), 202
    if set(body) != {'action'}:
        abort(400, description='控制动作参数无效')
    if action in {'pause', 'cancel', 'resume'}:
        service().control(identifier, action)
    elif action == 'delete_cache':
        service().delete_cache(identifier)
    else:
        abort(400, description='操作无效')
    return jsonify(service().snapshot(identifier)), 202


@youtube_tools.route('/api/tools/youtube/<identifier>/assets/<key>', methods=['GET', 'POST'])
def asset(identifier, key):
    """预览或保存经核验文件；参数：明确资源和确认；返回：文件流。"""
    saving = request.method == 'POST'
    resource = service().snapshot(identifier)['resources'].get(key)
    if not resource:
        abort(404, description='素材尚未准备')
    if saving and request.get_json() != ({'reviewed': True} if resource['kind'] == 'video' else {'save': True}):
        abort(400, description='请先预览并核对所选版本')
    path, name, mime = service().file(identifier, key)
    return send_file(path, mimetype=mime, conditional=not saving, as_attachment=saving, download_name=name)
