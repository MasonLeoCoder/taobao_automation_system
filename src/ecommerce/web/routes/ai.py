"""本机AI设置及有界后台连接检查；状态仅存在当前Web进程内。"""
from concurrent.futures import ThreadPoolExecutor
import secrets
from threading import Lock
from urllib.parse import urlsplit
from flask import Blueprint, abort, current_app, jsonify, render_template, request
from ecommerce.modules.ai_service import configuration, check_connection
from ecommerce.infrastructure.ai.client import AIError, create_client
from ecommerce.infrastructure.ai.config import load_config

ai = Blueprint('ai', __name__)


class ConnectionCheck:
    """每个应用最多一个检查，不排队、不自动恢复、不自动重试。"""
    def __init__(self):
        """建立单工作线程和状态锁；参数：无；返回：无。"""
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ai-check')
        self.lock = Lock()
        self.future = None

    def start(self, settings, provider, model):
        """有界提交；参数：应用配置和选择；返回：是否开始。"""
        with self.lock:
            if self.future and not self.future.done():
                return False
            self.future = self.pool.submit(self.run, settings, provider, model)
            return True

    @staticmethod
    def run(settings, provider, model):
        """执行并隐藏异常细节；参数：配置和选择；返回：成功或安全错误。"""
        try:
            return check_connection(settings, provider=provider, model=model)
        except (AIError, ValueError) as error:
            return {'status': 'failed', 'message': str(error)}
        except Exception:
            return {'status': 'failed', 'message': '连接检查异常，请检查本机配置；异常内容未显示。'}

    def status(self):
        """读取当前检查；参数：无；返回：内存状态。"""
        with self.lock:
            if self.future is None:
                return {'status': 'idle', 'message': '尚未测试。'}
            if not self.future.done():
                return {'status': 'running', 'message': '正在连接，请稍候…'}
            return self.future.result()


@ai.get('/settings/ai')
def index():
    """展示配置状态；参数：无；返回：页面，不访问AI接口。"""
    settings = current_app.config['SETTINGS']
    return render_template('ai_settings.html', active='ai', shop_name=settings.shop_name,
                           data=configuration(settings), token=current_app.config['COLLECTION_TOKEN'])


@ai.get('/api/ai/check')
def status():
    """读取当前进程的检查状态；参数：无；返回：JSON。"""
    return jsonify(current_app.extensions['ai_check'].status())


@ai.post('/api/ai/check')
def start():
    """校验本机来源和明确计费授权后提交；参数：JSON；返回：202或安全错误。"""
    if urlsplit(request.host_url).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        abort(403)
    if not secrets.compare_digest(request.headers.get('X-Collection-Token', ''), current_app.config['COLLECTION_TOKEN']):
        abort(403)
    origin = request.headers.get('Origin')
    if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
        abort(403)
    if not request.is_json:
        return jsonify(error='需要JSON请求。'), 415
    values = request.get_json()
    if not isinstance(values, dict) or set(values) - {'provider', 'model', 'allow_paid'} or values.get('allow_paid') is not True:
        return jsonify(error='请明确勾选允许一次可能计费的连接测试。'), 400
    settings = current_app.config['SETTINGS']
    try:
        data = configuration(settings)
        if not data['key_configured']:
            raise ValueError('尚未配置密钥，请在本机config/ai.toml的[ai]中填写api_key并刷新页面。')
        for field in ('provider', 'model'):
            if field in values and not isinstance(values[field], str):
                raise ValueError('服务商和模型须为文字。')
        create_client(load_config(settings.root), provider=values.get('provider'), model=values.get('model'))
    except (ValueError, AIError) as error:
        return jsonify(error=str(error)), 400
    if not current_app.extensions['ai_check'].start(settings, values.get('provider'), values.get('model')):
        return jsonify(error='已有连接检查正在进行，请等待完成。'), 409
    return jsonify(status='running', message='正在连接，请稍候…'), 202
