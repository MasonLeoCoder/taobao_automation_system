"""本机视频工具接口；请求仅派发任务与传输已校验缓存。"""
import secrets
from urllib.parse import urlsplit
from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

video_tools=Blueprint('video_tools',__name__)


def service():
    """取得当前应用业务服务；参数：无；返回：视频工具服务。"""
    return current_app.extensions['video_tools']


@video_tools.before_request
def protect():
    """核对本机来源与写请求令牌；参数：请求上下文；返回：无。"""
    if urlsplit(request.host_url).hostname not in {'localhost','127.0.0.1','::1'}:
        abort(403,description='视频工具仅供本机使用')
    if request.method=='POST':
        origin=request.headers.get('Origin')
        if origin and origin.rstrip('/')!=request.host_url.rstrip('/'):
            abort(403,description='不接受其他网站发起的请求')
        if not secrets.compare_digest(request.headers.get('X-Collection-Token',''),current_app.config['COLLECTION_TOKEN']):
            abort(403,description='页面已过期，请刷新后重试')
        if not request.is_json:
            abort(415,description='请求需要JSON内容')


@video_tools.errorhandler(ValueError)
def invalid(error):
    """返回业务输入错误；参数：异常；返回：JSON。"""
    return jsonify(error=str(error)),422


@video_tools.errorhandler(LookupError)
def missing(error):
    """返回缓存不存在；参数：异常；返回：JSON。"""
    return jsonify(error=str(error)),404


@video_tools.errorhandler(HTTPException)
def http_error(error):
    """返回可读接口错误；参数：HTTP异常；返回：JSON。"""
    return jsonify(error=error.description),error.code


@video_tools.get('/tools/douyin')
def page():
    """展示单条抖音工具；参数：无；返回：页面。"""
    return render_template('tools/douyin.html',active='tools',shop_name=current_app.config['SETTINGS'].shop_name)


@video_tools.route('/api/tools/douyin',methods=['GET','POST'])
def jobs():
    """查询历史或派发任务；参数：输入及登录开关；返回：状态JSON。"""
    if request.method=='GET':
        return jsonify(items=service().recent())
    body=request.get_json()
    if not isinstance(body,dict) or set(body)-{'input','interactive'} or not isinstance(body.get('input'),str) or type(body.get('interactive',False)) is not bool:
        abort(400,description='请输入一个抖音链接')
    return jsonify(id=service().start(body['input'],body.get('interactive',False))),202


@video_tools.route('/api/tools/douyin/<identifier>',methods=['GET','POST'])
def job(identifier):
    """查询或取消任务；参数：缓存编号；返回：状态JSON。"""
    if request.method=='POST':
        if request.get_json()!={'action':'cancel'}:
            abort(400,description='取消操作无效')
        service().cancel(identifier)
    return jsonify(service().snapshot(identifier))


@video_tools.route('/api/tools/douyin/<identifier>/media',methods=['GET','POST'])
def media(identifier):
    """预览或用户确认后保存；参数：缓存编号、确认值；返回：完整校验的视频流。"""
    saving=request.method=='POST'
    if saving and request.get_json()!={'reviewed':True}:
        abort(400,description='请先预览并确认画面无抖音水印')
    path,name=service().file(identifier)
    return send_file(path,mimetype='video/mp4',conditional=not saving,as_attachment=saving,download_name=name)
