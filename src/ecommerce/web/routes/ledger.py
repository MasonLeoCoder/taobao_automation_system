"""手动经营账本页面与本机表单提交。"""
import secrets
from urllib.parse import urlsplit
from uuid import uuid4
from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from ecommerce.modules.ledger import service
from ecommerce.infrastructure.storage import ledger as repository
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.web.workspace_plan import menu_data

ledger = Blueprint('ledger', __name__)


@ledger.before_request
def protect_request():
    """核对后端及本机提交来源；参数：当前请求；返回：拒绝或继续。"""
    if current_app.config['SETTINGS'].collection_backend != 'mysql':
        abort(409, description='经营账本需要启用MySQL数据库')
    if request.method == 'POST':
        if urlsplit(request.host_url).hostname not in {'localhost', '127.0.0.1', '::1'}:
            abort(403)
        if request.headers.get('Origin') and request.headers['Origin'].rstrip('/') != request.host_url.rstrip('/'):
            abort(403)
        if not secrets.compare_digest(request.form.get('token', '').encode('utf-8'), current_app.config['COLLECTION_TOKEN'].encode('utf-8')):
            abort(403, description='页面已失效，请刷新后重试')


def page(error=None, form=None, categories_mode=False):
    """渲染账本和错误原输入；参数：错误、表单、分类模式；返回：HTML。"""
    settings = current_app.config['SETTINGS']
    selected = request.values.get('month') or database_now().strftime('%Y-%m')
    filters = {key: request.args.get(key, default) for key, default in
               (('category', ''), ('direction', ''), ('status', 'active'), ('page', '1'))}
    data = service.overview(settings, selected, **filters)
    entry = form
    if entry is None and request.args.get('edit'):
        entry = repository.get_entry(settings, request.args['edit'])
        if not entry:
            abort(404)
        if entry['status'] != 'active':
            raise ValueError('作废账目不能修改')
        entry['occurred_at'] = entry['occurred_at'].isoformat(timespec='seconds')
    entry = {**dict(id=uuid4().hex, revision=0, occurred_at=database_now().isoformat(timespec='seconds'),
                    direction='expense', category_id='', amount='', note=''), **(entry or {})}
    return render_template('ledger.html', **data, entry=entry, selected_month=selected,
        entry_open=bool(form is not None or request.args.get('edit') or request.args.get('new') == '1'),
        filters=filters, categories_mode=categories_mode, error=error, active='ledger',
        title=menu_data()['items']['ledger']['name'], shop_name=settings.shop_name)


@ledger.errorhandler(ValueError)
def invalid(error):
    """展示输入错误而不丢弃内容；参数：异常；返回：400页面。"""
    if request.method == 'POST' and request.accept_mimetypes.best == 'application/json':
        return jsonify(error=str(error)), 400
    if request.method == 'POST':
        try:
            return page(str(error), request.form if request.form.get('action') == 'entry' else None,
                        request.form.get('action') == 'category'), 400
        except ValueError:
            pass
    return render_template('workspace_access_error.html', message=str(error), unavailable=False,
        active='ledger', shop_name=current_app.config['SETTINGS'].shop_name), 400


@ledger.get('/plans/ledger')
def index():
    """展示账本；参数：月份筛选；返回：经营账本页面。"""
    return page()


@ledger.get('/ledger/categories')
def categories():
    """展示分类管理；参数：月份；返回：分类表单页面。"""
    return page(categories_mode=True)


@ledger.post('/ledger/write')
def write():
    """处理短事务写入；参数：受保护表单；返回：重定向，刷新不重复新增。"""
    settings, action = current_app.config['SETTINGS'], request.form.get('action')
    selected = request.form.get('month', '')
    service.month(selected)
    if action == 'entry':
        service.save_entry(settings, request.form)
    elif action == 'void':
        repository.void_entry(settings, request.form.get('id', ''), service.integer(request.form.get('revision', ''), 1))
    elif action == 'category':
        service.save_category(settings, request.form)
    else:
        raise ValueError('不支持的操作')
    target = url_for('ledger.categories' if action == 'category' else 'ledger.index', month=selected)
    if request.accept_mimetypes.best == 'application/json':
        return jsonify(redirect=target)
    return redirect(target, code=303)
