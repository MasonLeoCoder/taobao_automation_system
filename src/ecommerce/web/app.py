"""本机 Web 服务，第一阶段只提供展示与查询。"""
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
    app = Flask(__name__)
    app.config.update(SETTINGS=settings, MAX_CONTENT_LENGTH=1024 * 1024)
    from ecommerce.web.routes.pages import pages
    app.register_blueprint(pages)
    from ecommerce.infrastructure.storage.mysql import MySQLStorageError

    @app.errorhandler(MySQLStorageError)
    def storage_unavailable(error):
        """明确返回存储故障；参数：安全异常；返回：503 JSON，不伪造空数据。"""
        return {'error':str(error),'storage':'unavailable'},503

    @app.template_filter("local_time")
    def local_time(value):
        """展示上海时间；参数：ISO 时间或空值；返回：显示文本。"""
        return datetime.fromisoformat(value).astimezone(BEIJING).strftime("%m-%d %H:%M:%S") if value else "—"

    @app.after_request
    def response_headers(response):
        """设置响应头；参数：响应对象；返回：响应对象。"""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    return app
