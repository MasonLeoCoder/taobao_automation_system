"""本机 Web 服务，提供资料查询与可配置的后台采集入口。"""
from datetime import datetime
from ecommerce.shared.time import BEIJING
from flask import Flask
from ecommerce.infrastructure.storage.database import initialize
from ecommerce.shared.config import Settings, load_settings, prepare_workspace


def create_app(settings: Settings | None = None) -> Flask:
    """创建 Web 应用；参数：可选配置；返回：Flask 应用。"""
    settings = settings or load_settings()
    prepare_workspace(settings)
    initialize(settings.task_database)
    if settings.task_backend=='mysql' and settings.collection_backend=='mysql':
        from ecommerce.modules.collection.dispatch import on_web_start
        on_web_start(settings)
    app = Flask(__name__)
    app.config.update(SETTINGS=settings, MAX_CONTENT_LENGTH=1024 * 1024)
    from ecommerce.web.routes.pages import pages
    app.register_blueprint(pages)
    from ecommerce.web.routes.analysis import analysis
    app.register_blueprint(analysis)
    import secrets
    app.config['COLLECTION_TOKEN']=secrets.token_hex(32)
    from ecommerce.web.routes.collection_jobs import collection_jobs
    app.register_blueprint(collection_jobs)
    from ecommerce.web.navigation import current_location,safe_return
    app.add_template_global(current_location)
    app.add_template_global(safe_return)
    from ecommerce.web.batch_presentation import batch_scope_text
    app.add_template_global(batch_scope_text)
    @app.template_global()
    def pagination_parameters(parameters=None,page_key='page'):
        """保留筛选并移除旧游标；参数：覆盖条件、页码键；返回：分页链接参数。"""
        from flask import request
        values=request.args.to_dict()
        for key in ('after','before','limit','range',page_key):
            values.pop(key,None)
        values.update(parameters or {})
        return {key:value for key,value in values.items() if value is not None}

    @app.template_global()
    def pagination_url(number,parameters=None,page_key='page'):
        """生成保留筛选的页码链接；参数：页码、条件和页码键；返回：站内链接。"""
        from flask import request
        from urllib.parse import urlencode
        return request.path+'?'+urlencode({**pagination_parameters(parameters,page_key),page_key:number})

    @app.template_global()
    def sort_url(field,spec,parameters=None,page_key='page',sort_key='sort',order_key='order'):
        """切换排序并回到首屏；参数：字段、当前排序、筛选及独立控件键；返回：站内链接。"""
        from flask import request
        from urllib.parse import urlencode
        direction=('desc' if spec['order']=='asc' else 'asc') if spec and spec.get('sort')==field else ('desc' if field.endswith('_at') or field=='sales' else 'asc')
        return request.path+'?'+urlencode({**pagination_parameters(parameters,page_key),sort_key:field,order_key:direction,page_key:1})
    from ecommerce.infrastructure.storage.mysql import MySQLStorageError

    @app.errorhandler(MySQLStorageError)
    def storage_unavailable(error):
        """明确返回存储故障；参数：安全异常；返回：503 JSON，不伪造空数据。"""
        return {'error':str(error),'storage':'unavailable'},503

    @app.template_filter("local_time")
    def local_time(value):
        """展示上海时间；参数：ISO 时间或空值；返回：显示文本。"""
        return datetime.fromisoformat(value).astimezone(BEIJING).strftime("%m-%d %H:%M:%S") if value else "—"

    @app.template_filter('full_local_time')
    def full_local_time(value):
        """展示完整北京时间；参数：带时区ISO时间；返回：含年份的日期时间或空标记。"""
        return datetime.fromisoformat(value).astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S') if value else '—'

    @app.after_request
    def response_headers(response):
        """设置响应头；参数：响应对象；返回：响应对象。"""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https://img.alicdn.com; media-src 'self' https://cloud.video.taobao.com; frame-ancestors 'none'; base-uri 'self'"
        return response

    return app
