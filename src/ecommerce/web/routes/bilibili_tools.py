"""B站下载接口，只接受本机带令牌的明确范围操作。"""
from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from .video_tools import protect, invalid, missing, http_error

bilibili_tools = Blueprint('bilibili_tools', __name__)
bilibili_tools.before_request(protect)
bilibili_tools.register_error_handler(ValueError, invalid)
bilibili_tools.register_error_handler(LookupError, missing)
bilibili_tools.register_error_handler(HTTPException, http_error)


@bilibili_tools.errorhandler(OSError)
def file_error(error):
    """将磁盘异常转为明确失败；参数：系统异常；返回：JSON，不泄露路径。"""
    return jsonify(error='文件读写失败，请检查磁盘空间、目录权限或文件是否正在使用'), 422


def service():
    """返回当前平台服务；参数：无；返回：B站工具。"""
    return current_app.extensions['bilibili_tools']


def body_only(keys):
    """校验请求字段；参数：允许字段集合；返回：字典。"""
    body = request.get_json()
    if not isinstance(body, dict) or set(body) - keys:
        abort(400, description='请求参数无效，请刷新页面后重试')
    return body


@bilibili_tools.get('/tools/bilibili')
def page():
    """展示B站工作台；参数：无；返回：页面。"""
    return render_template('tools/bilibili.html', active='tools', shop_name=current_app.config['SETTINGS'].shop_name)


@bilibili_tools.route('/api/tools/bilibili', methods=['GET', 'POST'])
def jobs():
    """读取历史或解析单个入口；参数：链接和登录模式；返回：摘要。"""
    if request.method == 'GET':
        return jsonify(items=service().recent(), **service().disk())
    body = body_only({'input', 'interactive'})
    if not isinstance(body.get('input'), str) or type(body.get('interactive', False)) is not bool:
        abort(400, description='请输入一个B站视频链接')
    return jsonify(id=service().start(body['input'], body.get('interactive', False))), 202


@bilibili_tools.post('/api/tools/bilibili/settings')
def settings():
    """更新本库的高级设置；参数：缓存目录、提醒容量、期限；返回：生效配置。"""
    body = body_only({'temp_dir', 'cache_warn_gib', 'idle_hours'})
    return jsonify(options=service().configure(body), **{'message': '设置已生效；已有记录的缓存仍位于原目录'})


@bilibili_tools.post('/api/tools/bilibili/cleanup')
def cleanup():
    """清理过期终态缓存；参数：固定动作；返回：数量与磁盘信息。"""
    if request.get_json() != {'action': 'cleanup'}:
        abort(400, description='清理动作无效')
    return jsonify(cleaned=service().cleanup(), **service().disk())


@bilibili_tools.route('/api/tools/bilibili/<identifier>', methods=['GET', 'POST'])
def job(identifier):
    """读取摘要或执行明确动作；参数：记录编号、动作和固定规格；返回：状态。"""
    if request.method == 'GET':
        return jsonify(service().snapshot(identifier))
    body = body_only({'action', 'episode_id', 'video_id', 'audio_id', 'kind', 'scope', 'output_format'})
    action = body.get('action')
    for key, value in body.items():
        if not isinstance(value, str) or len(value) > 200:
            abort(400, description='动作参数格式无效')
    if action == 'select' and set(body) == {'action', 'episode_id'}:
        service().select(identifier, body['episode_id'])
    elif action == 'prepare':
        if not {'episode_id', 'video_id'} <= set(body):
            abort(400, description='请明确所选分集和规格')
        key = service().prepare(identifier, **{k: v for k, v in body.items() if k != 'action'})
        return jsonify(key=key), 202
    elif action in {'pause', 'cancel', 'resume'} and set(body) == {'action'}:
        service().control(identifier, action)
    elif action == 'delete_cache' and set(body) == {'action'}:
        service().delete_cache(identifier)
    else:
        abort(400, description='操作无效')
    return jsonify(service().snapshot(identifier)), 202


@bilibili_tools.route('/api/tools/bilibili/<identifier>/assets/<key>', methods=['GET', 'POST'])
def asset(identifier, key):
    """预览或导出已核验文件；参数：固定资源编号；返回：支持Range的文件流。"""
    saving = request.method == 'POST'
    row = service().snapshot(identifier)
    resource = row['resources'].get(key)
    if not resource:
        abort(404, description='素材尚未准备')
    if saving and request.get_json() != ({'reviewed': True} if resource['kind'] == 'video' else {'save': True}):
        abort(400, description='请先确认所选内容与规格再保存')
    path, name, mime = service().file(identifier, key)
    return send_file(path, mimetype=mime, conditional=not saving, as_attachment=saving, download_name=name)
