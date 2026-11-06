"""本机发布类目管理与采集入口。"""
from secrets import compare_digest
from csv import writer
from io import StringIO
from urllib.parse import urlsplit

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request
from ecommerce.infrastructure.storage import publish_categories as repository
from ecommerce.modules.product.category_workbench import PILOT
from ecommerce.modules.product.listing_profiles import PROFILES, support
from ecommerce.shared.exceptions import TaskBusy

publish_categories = Blueprint('publish_categories', __name__)


@publish_categories.before_request
def protect():
    if current_app.config['SETTINGS'].collection_backend != 'mysql':
        abort(409, description='发布类目管理需要启用 MySQL')
    if request.method == 'POST':
        if (urlsplit(request.host_url).hostname not in {'localhost', '127.0.0.1', '::1'}
                or request.headers.get('Origin', request.host_url).rstrip('/') != request.host_url.rstrip('/')
                or not compare_digest(request.headers.get('X-Collection-Token', ''), current_app.config['COLLECTION_TOKEN'])):
            abort(403)


@publish_categories.errorhandler(ValueError)
@publish_categories.errorhandler(TaskBusy)
def rejected(error):
    return jsonify(error=str(error)), 409


def _data():
    return repository.catalog(current_app.config['SETTINGS'])


@publish_categories.get('/publish-categories')
def index():
    rows, scans = _data()
    for row in rows:
        row['support'] = support(dict(row, is_visible=True))
        profile = PROFILES.get(row['listing_profile_key'])
        row['profile_name'] = profile.label if profile else ''
    by_path = {row['full_path']: row for row in rows}
    current = by_path.get(request.args.get('branch', ''))
    branch = current['full_path'] if current else ''
    query = request.args.get('q', '').strip()
    needle = query.casefold()
    status = request.args.get('support', '')
    visibility = request.args.get('visibility', '')
    history = request.args.get('view') == 'history'
    roots = [row for row in rows if row['level'] == 1]
    parent_id = current['id'] if current else None
    if query or status or visibility:
        selected = [row for row in rows
                    if (not branch or row['full_path'].startswith(branch + repository.SEPARATOR))
                    and needle in row['full_path'].casefold()
                    and (not status or status == row['support'] or status == '末级类目' and row['is_leaf'])
                    and (visibility != 'hidden' or not row['is_visible'])
                    and (visibility != 'visible' or row['is_visible'])]
    else:
        selected = [row for row in rows if row['parent_id'] == parent_id]
    pages = max(1, (len(selected) + 49) // 50)
    page = min(pages, max(1, request.args.get('page', 1, type=int)))
    tree = {}
    for row in rows:
        tree.setdefault(row['parent_id'], []).append(row)
    crumbs = []
    parts = branch.split(repository.SEPARATOR) if branch else []
    for length in range(1, len(parts) + 1):
        path = repository.SEPARATOR.join(parts[:length])
        if path in by_path:
            crumbs.append(by_path[path])
    return render_template('publish_categories.html', active='publish-categories', roots=roots, tree=tree,
        rows=selected[(page - 1) * 50:page * 50], scans=scans, branch=branch, pilot=PILOT,
        page=page, pages=pages, total=len(selected), current=current, crumbs=crumbs, history=history,
        full=repository.has_complete_branch(current_app.config['SETTINGS'], PILOT),
        query=query, selected_status=status, visibility=visibility,
        shop_name=current_app.config['SETTINGS'].shop_name)


@publish_categories.post('/api/publish-categories/scan')
def scan():
    data = request.get_json()
    scopes = data.get('scopes') if isinstance(data, dict) else None
    if (not isinstance(scopes, list) or len(scopes) > 2 or
            any(not isinstance(path, str) or not path for path in scopes) or len(set(scopes)) != len(scopes)):
        raise ValueError('请选择有效采集范围')
    rows, scans = _data()
    known = {row['full_path'] for row in rows if not row['is_leaf']}
    known.add(PILOT)
    for entry in scans:
        known.update(item['path'] for item in entry['result'].get('failed', []) if item['path'])
    if (not scopes and not repository.has_complete_branch(current_app.config['SETTINGS'], PILOT)) or set(scopes) - known:
        raise ValueError('请先完成宠物分支采集，再扩大采集范围')
    return jsonify(current_app.extensions['publish_categories'].start(scopes)), 202


@publish_categories.get('/publish-categories/export')
def export():
    rows, _ = _data()
    output = StringIO(newline='')
    csv = writer(output)
    csv.writerow(('本地类目 ID', '父级 ID', '千牛类目 ID', '完整路径', '层级', '是否末级',
                  '千牛可见', '上架支持', '最近确认时间'))
    for row in rows:
        path = row['full_path']
        if path.lstrip().startswith(('=', '+', '-', '@')):
            path = "'" + path
        csv.writerow((row['id'], row['parent_id'], row['qianniu_category_id'], path, row['level'],
            '是' if row['is_leaf'] else '否', '是' if row['is_visible'] else '否', support(row), row['last_seen_at']))
    return Response('\ufeff' + output.getvalue(), content_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="publish-categories.csv"'})


@publish_categories.get('/api/publish-categories/status')
def status():
    return jsonify(current_app.extensions['publish_categories'].snapshot())


@publish_categories.post('/api/publish-categories/resume')
def resume():
    data = request.get_json()
    if not isinstance(data, dict) or not isinstance(data.get('prompt'), str):
        raise ValueError('人工反馈无效')
    current_app.extensions['publish_categories'].resume(data['prompt'])
    return jsonify(current_app.extensions['publish_categories'].snapshot())
