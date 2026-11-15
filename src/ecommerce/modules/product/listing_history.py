"""按执行时的账号与店铺汇总网页和命令行上架记录。"""
import hashlib
import json
import re
import time

from ecommerce.infrastructure.storage.listing_history import cli_listing_events
from ecommerce.infrastructure.storage.time_values import iso_value
from ecommerce.infrastructure.tasks.ownership import owner_alive

LISTING_RESULTS = {'warehouse': '已入仓', 'uncertain': '结果待核对', 'filled': '已填写，未提交'}
LISTING_ENTRIES = {'web': '网页', 'cli': '命令行'}
DUPLICATE_NOTICE = '同一店铺和商家编码有入仓结果待核对。请先到千牛仓库人工核对，再决定是否提交本次商品。'
_ID = re.compile('[0-9a-f]{32}')


def read_listing_state(path):
    """兼容 Windows 上原子替换状态文件时的短暂占用。"""
    for attempt in range(3):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(.02 * (attempt + 1))


def listing_records(root):
    """读取已启动的网页上架摘要，兼容旧版导入记录。"""
    if not root.is_dir():
        return
    for path in root.iterdir():
        if not _ID.fullmatch(path.name) or path.resolve().parent != root.resolve():
            continue
        try:
            state = read_listing_state(path / 'state.json')
            task_id = state.get('task_id')
            if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
                continue
            manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
            preview = manifest['preview']
            outcome = state.get('outcome')
            fields = state.get('observed_fields', {})
            yield {'id': path.name, 'task_id': task_id, 'entry': 'web', 'target': manifest.get('listing_target'),
                   'title': fields.get('title', state.get('titles', {}).get('title', preview.get('title', ''))),
                   'code': state.get('submitted_code', fields.get('code', preview.get('code', ''))),
                   'item_id': state.get('item_id'), 'outcome': outcome,
                   'label': LISTING_RESULTS.get(outcome, '未确认填写完成'),
                   'created_at': state.get('created_at', ''), 'warehouse_at': state.get('warehouse_at'),
                   'used_at': state.get('warehouse_at') or state.get('filled_at') or state.get('created_at', ''),
                   'verified_identity': state.get('verified_identity'),
                   'waiting': state['phase'] == 'waiting' and bool(state.get('prompt'))
                              and owner_alive(state['pid'], state['process_started']) is not False}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue


def cli_listing_records(settings, *, shop_id=None, task_id=None):
    if settings.task_backend != 'mysql':
        return {}
    records = {}
    outcomes = {'listing_filled': 'filled', 'listing_submit_intent': 'uncertain', 'listing_warehouse': 'warehouse'}
    for event in cli_listing_events(settings, shop_id=shop_id, task_id=task_id):
        identifier = event['task_id']
        details = event['details']
        outcome = outcomes[event['event_type']]
        occurred_at = iso_value(event['occurred_at'])
        records[identifier] = {
            'id': None, 'task_id': identifier, 'entry': 'cli', 'waiting': False,
            'target': (event['source_details'] or {}).get('qianniu_source'),
            'title': details.get('title'), 'code': details.get('code'), 'code_sha256': details.get('code_sha256'),
            'item_id': details.get('item_id'),
            'outcome': outcome, 'label': LISTING_RESULTS[outcome], 'created_at': iso_value(event['started_at']),
            'warehouse_at': occurred_at if outcome == 'warehouse' else None,
            'used_at': occurred_at, 'verified_identity': details.get('verified_identity'),
        }
    return records


def code_fingerprint(code):
    return hashlib.sha256(code.encode('utf-8')).hexdigest()


def pending_submissions(settings, shop_id, code, *, exclude_task):
    """仅按同店实际商品级商家编码寻找需要人工核对的提交。"""
    if not code:
        return []
    root = settings.workspace / 'storage' / 'product_listing'
    records = (*listing_records(root), *cli_listing_records(settings, shop_id=shop_id).values())
    fingerprint = code_fingerprint(code)
    matching = {row['task_id']: row['created_at'] for row in records
                if row['task_id'] != exclude_task and row['outcome'] == 'uncertain'
                and (row['target'] or {}).get('shop_id') == shop_id
                and (row['code_sha256'] == fingerprint if row.get('code_sha256') else row['code'] == code)}
    return sorted(matching, key=lambda task_id: (matching[task_id], task_id), reverse=True)
