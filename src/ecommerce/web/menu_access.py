"""Web功能启停的集中路由归属；隐藏不拦访问，停用不启动新业务动作。"""
from flask import current_app, g, jsonify, render_template, request
from ecommerce.infrastructure.storage import web_menus
from ecommerce.modules.workspace_menu.service import MenuConfigurationError, enabled
from ecommerce.web.workspace_plan import ITEMS, menu_data

DIRECT = {
    'pages.home': 'home', 'pages.status': 'home', 'pages.tasks': 'tasks', 'pages.task_detail': 'tasks',
    'pages.logs': 'logs', 'pages.events': 'logs', 'pages.module': 'collection',
    'pages.collection_searches': 'searches', 'pages.collection_keyword_products': 'searches',
    'pages.collection_search': 'searches', 'pages.collection_details': 'catalog',
    'pages.collection_product': 'catalog', 'pages.collection_products': 'catalog',
    'image_tools.main_images': 'catalog', 'image_tools.detail_images': 'catalog',
}
BLUEPRINTS = {'ledger': 'ledger', 'analysis': 'analysis', 'favorites': 'favorites', 'ai': 'ai',
              'review_analysis': 'catalog', 'question_analysis': 'catalog', 'composite_analysis': 'catalog',
              'image_analysis': 'catalog', 'image_modules': 'catalog',
              'image_tools': 'tools', 'video_tools': 'tools', 'bilibili_tools': 'tools', 'youtube_tools': 'tools',
              'product_listing': 'listing', 'publish_categories': 'publish-categories', 'sycm_search': 'sycm-search'}
SAFE_STATUS = {'collection_jobs.job_status', 'collection_jobs.status', 'collection_jobs.active',
               'collection_jobs.recent', 'collection_jobs.history', 'collection_jobs.check_update_submission',
               'review_analysis.status', 'publish_categories.status', 'sycm_search.status'}
CONTROLS = {'collection_jobs.control', 'collection_jobs.job_control'}
VIDEO_JOBS = {'video_tools.job', 'bilibili_tools.job', 'youtube_tools.job'}


def task_safety_request():
    """识别只读状态和明确停止请求；参数：当前请求；返回：是否保留原有令牌和归属校验通道。"""
    endpoint = request.endpoint
    if request.method in ('GET', 'HEAD'):
        return endpoint in SAFE_STATUS or endpoint in VIDEO_JOBS or endpoint == 'collection_jobs.retry_queue'
    if request.method != 'POST':
        return False
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return False
    if endpoint in CONTROLS:
        return set(body) <= {'action', 'prompt_id'} and isinstance(body.get('action'), str) and body['action'] in {'stop', 'pause'}
    if endpoint in VIDEO_JOBS:
        return set(body) == {'action'} and isinstance(body['action'], str) and body['action'] in {'pause', 'cancel'}
    return endpoint == 'collection_jobs.retry_queue' and body == {'action': 'pause'}


def requirements(data):
    """映射页面、接口及具体资源；参数：本请求配置；返回：必须同时启用的功能标识。"""
    endpoint = request.endpoint
    if endpoint == 'workspace.plan':
        key = request.view_args['key']
        return (key,) if key in ITEMS and ITEMS[key]['planned'] else ()
    if endpoint == 'pages.collections':
        key = 'catalog' if request.args.get('product_id') else ('searches' if request.args.get('view') == 'searches' else 'catalog')
        return (key,)
    if endpoint == 'pages.collection_batches':
        module = request.args.get('module')
        return ('searches',) if module == 'list' else ('catalog',) if module else ('searches', 'catalog')
    if endpoint in {'pages.collection_records', 'pages.collection_batch', 'pages.collection_file'}:
        # 两类功能同启用时无需额外读取资源；不同时必须按实际来源判定，不能用URL前缀借道。
        if enabled(data, 'catalog') and enabled(data, 'searches'):
            return ('catalog',)
        owners = web_menus.resource_owners(current_app.config['SETTINGS'].mysql,
            **({'file_id': request.view_args['file_id']} if endpoint == 'pages.collection_file'
               else {'batch_id': request.view_args['batch_id']}))
        if any(enabled(data, key) for key in owners):
            return ()
        return tuple(sorted(owners or {'catalog'}))
    if endpoint in DIRECT:
        return (DIRECT[endpoint],)
    if request.blueprint == 'collection_jobs':
        # 商品页中的新采集动作同时受商品库及采集开关约束，通用采集入口归采集功能。
        return ('collection', 'catalog') if request.view_args.get('product_id') is not None else ('collection',)
    if request.blueprint in BLUEPRINTS:
        return (BLUEPRINTS[request.blueprint],)
    raise MenuConfigurationError('该Web入口尚未登记功能归属，未执行操作')


def access_error(message, code, *, unavailable=False):
    """返回安全的停用或配置错误；参数：文案、状态码及读取失败标记；返回：JSON或HTML响应。"""
    if unavailable:
        g.workspace_menu_error = True
    if request.path.startswith('/api/'):
        return jsonify(error=message, code='menu_unavailable' if unavailable else 'feature_disabled'), code
    return render_template('workspace_access_error.html', message=message, unavailable=unavailable,
        active=None, shop_name=current_app.config['SETTINGS'].shop_name), code


def install(app):
    """接入请求级启停守卫；参数：Flask应用；返回：无，不替代已有来源、令牌与业务归属校验。"""
    @app.before_request
    def check_menu_access():
        """请求执行前核对有效状态；参数：无；返回：停用响应或继续原处理。"""
        if request.endpoint in (None, 'static', 'pages.health') or task_safety_request():
            return None
        data = menu_data()
        for key in requirements(data):
            if not enabled(data, key):
                item = data['items'].get(key)
                name = item['name'] if item else '该功能'
                group = data['groups'][item['group_key']] if item else None
                reason = ('所属分组“'+group['name']+'”已停用' if group and not group['is_enabled'] else '已停用')
                return access_error(name+reason+'，请在数据库中启用后访问。', 403)
        return None

    @app.errorhandler(MenuConfigurationError)
    def menu_configuration_error(error):
        """配置故障不能改用默认状态放行；参数：安全错误；返回：503。"""
        return access_error(str(error), 503, unavailable=True)
