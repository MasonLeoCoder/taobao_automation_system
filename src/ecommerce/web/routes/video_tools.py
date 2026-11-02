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
        items=service().recent()
        return jsonify(items=items,cache_bytes=service().usage())
    body=request.get_json()
    if not isinstance(body,dict) or set(body)-{'input','interactive'} or not isinstance(body.get('input'),str) or type(body.get('interactive',False)) is not bool:
        abort(400,description='请输入一个抖音链接')
    return jsonify(id=service().start(body['input'],body.get('interactive',False))),202


@video_tools.route('/api/tools/douyin/<identifier>',methods=['GET','POST'])
def job(identifier):
    """查询或取消任务；参数：缓存编号；返回：状态JSON。"""
    if request.method=='POST':
        body=request.get_json()
        if not isinstance(body,dict):
            abort(400,description='素材操作无效')
        if body=={'action':'cancel'}:
            service().cancel(identifier)
        elif body=={'action':'delete_cache'}:
            service().delete_cache(identifier)
        elif body.get('action')=='prepare' and set(body)<={'action','kind','format_id','output_format'}:
            key=service().prepare(identifier,body.get('kind'),body.get('format_id'),body.get('output_format','original'))
            return jsonify(id=identifier,asset_id=key),202
        else:
            abort(400,description='素材操作无效')
    return jsonify(service().snapshot(identifier))


@video_tools.route('/api/tools/douyin/<identifier>/media',methods=['GET','POST'])
def media(identifier):
    """预览或用户确认后保存；参数：缓存编号、确认值；返回：完整校验的视频流。"""
    saving=request.method=='POST'
    if saving and request.get_json()!={'reviewed':True}:
        abort(400,description='请先预览并确认画面无抖音水印')
    path,name,mime=service().file(identifier)
    return send_file(path,mimetype=mime,conditional=not saving,as_attachment=saving,download_name=name)


@video_tools.route('/api/tools/douyin/<identifier>/assets/<key>',methods=['GET','POST'])
def asset(identifier,key):
    """预览或导出固定素材；参数：记录、资源编号和视频核对结果；返回：校验后文件流。"""
    saving=request.method=='POST'
    row=service().snapshot(identifier)
    resource=row.get('resources',{}).get(key)
    if not resource:
        abort(404,description='该素材尚未准备')
    if saving:
        body=request.get_json()
        if resource['kind']=='video' and body!={'reviewed':True}:
            abort(400,description='请先预览所选版本并确认无抖音水印')
        if resource['kind']!='video' and body!={'save':True}:
            abort(400,description='保存操作无效')
    path,name,mime=service().file(identifier,key)
    return send_file(path,mimetype=mime,conditional=not saving,as_attachment=saving,download_name=name)


@video_tools.post('/api/tools/douyin/cleanup')
def cleanup():
    """清理过期缓存；参数：固定动作；返回：占用字节。"""
    if request.get_json()!={'action':'cleanup'}:
        abort(400,description='清理操作无效')
    service().cleanup()
    return jsonify(cache_bytes=service().usage())
