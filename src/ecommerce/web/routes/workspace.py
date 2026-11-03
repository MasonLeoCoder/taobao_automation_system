"""功能规划只读页面；不接入采集、AI调用或经营数据写入。"""
from flask import Blueprint, abort, current_app, render_template
from ecommerce.web.workspace_plan import plan_page

workspace = Blueprint('workspace', __name__)


@workspace.get('/plans/<key>')
def plan(key):
    """展示规划页；参数：规划标识；返回：静态说明，未知标识返回404。"""
    page = plan_page(key)
    if page is None:
        abort(404)
    return render_template('workspace_plan.html', plan=page, active='plan:' + key,
                           shop_name=current_app.config['SETTINGS'].shop_name)
