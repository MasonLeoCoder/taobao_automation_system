"""长期关注页面及短事务接口；不访问平台，不在GET中修改关注。"""
from datetime import datetime
from decimal import Decimal
import secrets
from urllib.parse import urlsplit
from flask import Blueprint, abort, current_app, jsonify, render_template, request
from itsdangerous import URLSafeTimedSerializer, BadData
from ecommerce.modules.favorites import service
from ecommerce.infrastructure.storage import favorites as repository
from ecommerce.infrastructure.storage.collection_queries import public

favorites = Blueprint('favorites', __name__)


def money(value):
    """展示原列表价格；参数：数字或空；返回：至少两位小数并保留原精度。"""
    if value is None:
        return '—'
    number = Decimal(str(value))
    return '¥' + format(number, f'.{max(2, -number.normalize().as_tuple().exponent)}f')


@favorites.before_request
def protect_request():
    """核对本机来源和写令牌；参数：请求；返回：无或明确拒绝。"""
    if current_app.config['SETTINGS'].collection_backend != 'mysql':
        abort(409, description='采集数据库尚未启用')
    if request.method != 'POST':
        return
    if urlsplit(request.host_url).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        abort(403, description='仅允许本机关注操作')
    if not secrets.compare_digest(request.headers.get('X-Collection-Token', ''), current_app.config['COLLECTION_TOKEN']):
        abort(403, description='页面已失效，请刷新后重试')
    origin = request.headers.get('Origin')
    if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
        abort(403, description='不接受其他网站发起的关注操作')
    if not request.is_json:
        abort(415, description='关注操作需要JSON请求')


@favorites.errorhandler(ValueError)
@favorites.errorhandler(LookupError)
def invalid_request(error):
    """返回可理解的关注异常；参数：异常；返回：409 JSON。"""
    return jsonify(error=str(error)), 409


@favorites.get('/favorites')
def index():
    """展示长期关注分页；参数：URL筛选；返回：关注与来源对照页面。"""
    settings = current_app.config['SETTINGS']
    data = service.page(settings, request.args)
    from ecommerce.infrastructure.storage.search_analysis import attach_previews
    from ecommerce.modules.collection.queries import inspect_thumbnails
    rows = {r['id']: r for item in data['items'] for r in (item['initial'], item['latest']) if r}
    attach_previews(settings, list(rows.values()))
    inspect_thumbnails(settings, list(rows.values()))
    for item in data['items']:
        for key in ('initial', 'latest'):
            if item[key]:
                item[key] = rows[item[key]['id']]
    states = {str(item['product_id']): item['id'] for item in data['items']}
    return render_template('favorites.html', active='favorites', shop_name=settings.shop_name,
                           favorite_states=states, favorite_money=money, **public(data))


@favorites.post('/api/favorites')
def operate():
    """添加、备注、取消和10秒内撤销；参数：JSON操作；返回：已提交状态及签名撤销凭据。"""
    values = request.get_json()
    if not isinstance(values, dict):
        raise ValueError('关注参数必须为对象')
    settings = current_app.config['SETTINGS']
    product_id = service.identifier(values.get('product_id'))
    action = values.get('action')
    serializer = URLSafeTimedSerializer(current_app.config['COLLECTION_TOKEN'], salt='favorite-undo-v1')
    if action == 'add':
        row = service.add(settings, product_id, values.get('observation_id'))
    elif action in {'remove', 'note'}:
        expected = values.get('favorite_id')
        if not isinstance(expected, str) or len(expected) != 32:
            raise ValueError('关注版本缺失，请刷新页面')
        row = repository.change(settings, product_id, expected_id=expected, remove=action == 'remove',
            note=service.note_text(values.get('note')) if action == 'note' else None)
        if action == 'remove':
            return jsonify(product_id=product_id, active=False, undo=serializer.dumps(public(row)))
    elif action == 'undo':
        try:
            row = serializer.loads(values.get('undo', ''), max_age=10)
        except (BadData, TypeError):
            raise ValueError('撤销已过期，请重新关注') from None
        if row['product_id'] != product_id:
            raise ValueError('撤销商品不匹配')
        for key in ('created_at', 'updated_at'):
            row[key] = datetime.fromisoformat(row[key]).replace(tzinfo=None)
        row = repository.change(settings, product_id, restore=row)
    else:
        raise ValueError('关注操作无效')
    return jsonify(product_id=product_id, active=True, favorite_id=row['id'])
