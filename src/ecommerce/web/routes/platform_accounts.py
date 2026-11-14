"""本机千牛账号管理页面及扫码操作。"""

import re
from secrets import compare_digest
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, jsonify, render_template, request

from ecommerce.modules.account.web_accounts import MANAGEMENT_ACTIONS
from ecommerce.shared.exceptions import NeedsAttention, TaskBusy

platform_accounts = Blueprint('platform_accounts', __name__)


def workbench():
    return current_app.extensions['platform_accounts']


@platform_accounts.before_request
def protect():
    if request.remote_addr not in {'127.0.0.1', '::1'} or urlsplit(request.host_url).hostname not in {
        'localhost', '127.0.0.1', '::1',
    }:
        abort(403)
    if current_app.config['SETTINGS'].task_backend != 'mysql':
        abort(409, description='账号管理需要 MySQL')
    if request.method == 'POST' and (
        request.headers.get('Origin', request.host_url).rstrip('/') != request.host_url.rstrip('/')
        or not compare_digest(request.headers.get('X-Collection-Token', ''), current_app.config['COLLECTION_TOKEN'])
    ):
        abort(403)


@platform_accounts.errorhandler(ValueError)
@platform_accounts.errorhandler(LookupError)
@platform_accounts.errorhandler(TaskBusy)
@platform_accounts.errorhandler(NeedsAttention)
def rejected(error):
    return jsonify(error=str(error)), {LookupError: 404, TaskBusy: 409, NeedsAttention: 409}.get(type(error), 422)


@platform_accounts.get('/accounts')
def index():
    filters = {name: request.args.get(name, '').strip() for name in ('shop', 'login', 'status')}
    if filters['status'] not in ('enabled', 'disabled'):
        filters['status'] = ''
    all_accounts = workbench().accounts()
    shop, login, state = (filters[name].casefold() for name in ('shop', 'login', 'status'))
    accounts = [row for row in all_accounts
                if (shop in row['shop_name'].casefold() or shop in row['platform_shop_id'])
                and login in row['login_name'].casefold()
                and (not state or row['enabled'] == (state == 'enabled'))]
    return render_template('platform_accounts.html', active='platform-accounts',
        shop_name=current_app.config['SETTINGS'].shop_name, accounts=accounts, filters=filters, total=len(all_accounts))


@platform_accounts.get('/api/accounts/status')
def status():
    return jsonify(workbench().snapshot())


@platform_accounts.post('/api/accounts/start')
def start():
    payload = request.get_json(silent=True)
    if payload == {'action': 'add'}:
        return jsonify(workbench().start('add')), 202
    if (not isinstance(payload, dict) or set(payload) != {'action', 'account_id', 'shop_id', 'revision'}
        or not isinstance(payload['action'], str)
        or payload['action'] not in {'login', 'check', 'open'} | MANAGEMENT_ACTIONS
        or not isinstance(payload['account_id'], str) or not re.fullmatch('[0-9a-f]{32}', payload['account_id'])
        or not isinstance(payload['shop_id'], str) or not re.fullmatch('[0-9]{1,64}', payload['shop_id'])
        or type(payload['revision']) is not int or payload['revision'] < 0):
        raise ValueError('账号操作参数无效')
    return jsonify(workbench().start(payload['action'], payload['account_id'], payload['shop_id'], payload['revision'])), 202


@platform_accounts.post('/api/accounts/respond')
def respond():
    payload = request.get_json(silent=True)
    if (not isinstance(payload, dict) or set(payload) != {'id', 'action', 'prompt'}
        or payload['action'] not in ('continue', 'cancel')
        or not isinstance(payload['id'], str)
        or payload['prompt'] is not None and not isinstance(payload['prompt'], str)):
        raise ValueError('账号操作反馈无效')
    workbench().respond(payload['id'], payload['action'], payload['prompt'])
    return jsonify(workbench().snapshot())
