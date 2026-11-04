"""手动经营账本页面与本机表单提交。"""
import secrets
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4
from itsdangerous import BadData, URLSafeTimedSerializer
from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from ecommerce.modules.ledger import service
from ecommerce.infrastructure.storage import ledger as repository
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.web.workspace_plan import menu_data

ledger = Blueprint('ledger', __name__)


def notice_signer():
    """构造短期操作回执签名器；参数：无；返回：随当前网页进程令牌失效的签名器。"""
    return URLSafeTimedSerializer(current_app.config['COLLECTION_TOKEN'], salt='ledger-notice-v1')


def notice_message():
    """只展示近期成功操作的回执；参数：请求回执；返回：提示文字或空串。"""
    try:
        message = notice_signer().loads(request.args.get('receipt', ''), max_age=120)
        return message if isinstance(message, str) and len(message) <= 80 else ''
    except BadData:
        return ''


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


def page(error=None, form=None, categories_mode=False, limit_form=None):
    """渲染账本和错误原输入；参数：错误、表单、分类模式；返回：HTML。"""
    settings = current_app.config['SETTINGS']
    selected = request.values.get('month') or database_now().strftime('%Y-%m')
    filters = {key: request.args.get(key, default) for key, default in
               (('category', ''), ('direction', ''), ('status', 'active'), ('page', '1'))}
    data = service.overview(settings, selected, **filters)
    selected_label = f'{int(selected[:4])}年{int(selected[5:])}月'
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
    limit_open = limit_form is not None or request.args.get('edit_limit') == '1'
    return render_template('ledger.html', **data, entry=entry, selected_month=selected,
        selected_label=selected_label, notice=notice_message(),
        limit_open=limit_open,
        limit_value=limit_form.get('amount', '') if limit_form is not None else (data['budget'] if data['budget'] is not None else ''),
        entry_open=bool(not limit_open and (form is not None or request.args.get('edit') or request.args.get('new') == '1')),
        filters=filters, categories_mode=categories_mode, error=error, active='ledger',
        title=menu_data()['items']['ledger']['name'], shop_name=settings.shop_name)


@ledger.errorhandler(ValueError)
def invalid(error):
    """展示输入错误而不丢弃内容；参数：异常；返回：400页面。"""
    if request.method == 'POST' and request.accept_mimetypes.best == 'application/json':
        return jsonify(error=str(error)), 400
    if request.method == 'POST':
        try:
            action = request.form.get('action')
            return page(str(error), request.form if action == 'entry' else None,
                        action == 'category' or (action == 'limit' and request.form.get('return_view') == 'categories'),
                        limit_form=request.form if action == 'limit' else None), 400
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
    elif action == 'limit':
        service.save_limit(settings, request.form)
    else:
        raise ValueError('不支持的操作')
    endpoint = 'ledger.categories' if action == 'category' or (action == 'limit' and request.form.get('return_view') == 'categories') else 'ledger.index'
    target = url_for(endpoint, month=selected)
    message = ''
    if action == 'entry':
        occurred = datetime.fromisoformat(request.form['occurred_at'])
        destination = f'{occurred.year}年{occurred.month}月'
        message = f'已更新，归入{destination}' if int(request.form.get('revision', '0')) else f'已记入{destination}'
    elif action == 'void':
        message = '账目已作废'
    elif action == 'limit':
        message = f'{int(selected[:4])}年{int(selected[5:])}月止损线已保存'
    if message:
        target = url_for(endpoint, month=selected, receipt=notice_signer().dumps(message))
    if request.accept_mimetypes.best == 'application/json':
        return jsonify(redirect=target)
    return redirect(target, code=303)
