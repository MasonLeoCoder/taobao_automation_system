"""页面路由与只读状态接口。"""
from flask import Blueprint, abort, current_app, jsonify, render_template
from ecommerce.modules.monitoring.service import overview
from ecommerce.shared.features import FEATURES

pages = Blueprint("pages", __name__)


@pages.get("/")
def home():
    """展示工作台；参数：无；返回：首页响应。"""
    return render_template("dashboard.html", active="home", **overview(current_app.config["SETTINGS"]))


@pages.get("/tasks")
def tasks():
    """展示真实任务记录；参数：无；返回：任务页面。"""
    return render_template("tasks.html", active="tasks", **overview(current_app.config["SETTINGS"]))


@pages.get("/modules/<key>")
def module(key: str):
    """展示能力说明；参数：模块标识；返回：模块页面或 404。"""
    feature = next((item for item in FEATURES if item["key"] == key), None)
    if feature is None:
        abort(404)
    return render_template("module.html", active=key, feature=feature, **overview(current_app.config["SETTINGS"]))


@pages.get("/api/status")
def status():
    """返回运行状态；参数：无；返回：JSON 响应。"""
    return jsonify(overview(current_app.config["SETTINGS"]))


@pages.get("/health")
def health():
    """返回服务健康状态；参数：无；返回：JSON 响应。"""
    return jsonify(status="ok")
