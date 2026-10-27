"""参数资料按商品合并，逐批保留原始模块 HTML 与诊断记录。"""
import hashlib
import json
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection.detail_storage import product_directory, save_detail_section
from ecommerce.platforms.taobao.detail import matches_detail_url, resolve_detail_input
from ecommerce.platforms.taobao.detail_parameters import collect_parameter_data, read_parameters, snapshot_key
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention


class _ParametersChanged(Exception):
    """发布检查点期间参数发生变化，需重新确认快照。"""


def save_parameter_result(directory, record, raw_html, *, attempt=None):
    product_dir = directory.parent.parent
    directory.mkdir(parents=True, exist_ok=True)
    stem = 'parameters' if attempt is None else f'parameters-attempt-{attempt}'
    html_path = directory / f'{stem}.html'
    destination = directory / f'{stem}.json'
    saved = {**record, 'html_file': None, 'html_path': None, 'html_sha256': None, 'html_size_bytes': 0,
             'history_record_file': destination.relative_to(product_dir).as_posix()}
    if raw_html is not None:
        raw_bytes = raw_html.encode('utf-8')
        temporary = html_path.with_suffix('.html.tmp')
        try:
            temporary.write_bytes(raw_bytes)
            temporary.replace(html_path)
        finally:
            temporary.unlink(missing_ok=True)
        saved.update(html_file=html_path.relative_to(product_dir).as_posix(), html_path=str(html_path.resolve()),
                     html_sha256=hashlib.sha256(raw_bytes).hexdigest(), html_size_bytes=len(raw_bytes))
    temporary = destination.with_suffix('.json.tmp')
    try:
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    section = 'parameters' if saved['status'] in {'complete', 'no_parameters'} else 'parameters_last_attempt'
    record_path = save_detail_section(product_dir, record['product_id'], section, saved)
    return {**saved, 'record_path': str(record_path.resolve()), 'history_record_path': str(destination.resolve())}


def collect_parameters(settings, result, *, stable_ms=500):
    guard = active_guard()
    if guard is None:
        raise RuntimeError('参数采集必须在淘宝专用守卫中运行')
    item_id = result.target.item_id
    batch_id = guard.task_id + '-' + uuid4().hex[:8]
    from ecommerce.modules.collection.recovery import register_attempt
    register_attempt(settings,result.target.item_id,'parameters',batch_id,guard.task_id)
    directory = product_directory(settings, item_id) / 'history' / batch_id
    base = {'schema_version': 1, 'product_id': item_id, 'task_id': guard.task_id, 'batch_id': batch_id,
            'source_url': result.target.canonical_url, 'scope': 'product_parameters'}

    def check_target():
        if not matches_detail_url(result.page, item_id):
            raise NeedsAttention('保存参数前目标商品发生变化，未更新商品资料')

    def record_for(value, status):
        return {**base, 'captured_at': now(), 'status': status, 'state': value['state'],
                'items': value['items'], 'summary': value.get('summary', {'total_count': None, 'read_count': 0,
                    'empty_value_count': 0, 'error_count': None, 'unrecognized_fragment_count': None, 'total_count_confirmed': False}),
                'unknown_fragments': value['unknown_fragments'], 'reason': value.get('reason'),
                'explicit_empty_text': value.get('explicit_empty_text'),
                'expansion_count': value.get('expansion_count', 0), 'selector': value.get('selector', '.paramsInfoArea:visible')}

    diagnostic_count = 0
    def diagnostic(value, attempt):
        nonlocal diagnostic_count
        if not matches_detail_url(result.page, item_id):
            return
        diagnostic_count += 1
        save_parameter_result(directory, record_for(value, 'partial'), value.get('raw_html'), attempt=diagnostic_count)

    while True:
        value = collect_parameter_data(result, settings.timeout_ms, on_diagnostic=diagnostic, stable_ms=stable_ms)
        record = record_for(value, value['status'])
        record['diagnostic_count'] = diagnostic_count

        def save():
            try:
                current = read_parameters(result.page, item_id)
            except (ValueError, PlaywrightError) as error:
                raise _ParametersChanged from error
            if snapshot_key(current) != snapshot_key(value):
                raise _ParametersChanged
            check_target()
            record['source_url'] = resolve_detail_input(result.page.url).canonical_url
            return save_parameter_result(directory, record, value['raw_html'])

        try:
            saved = guard.perform('保存参数信息与原始 HTML', save)
            break
        except _ParametersChanged:
            guard.request_attention(result.page, '发布前参数内容或商品页面发生变化，请确认原商品参数后回复继续，将重新读取快照', step='复核参数保存')
    count = saved['summary']['total_count']
    message = f'参数采集完成：{count}项，空值{saved["summary"]["empty_value_count"]}项，异常{saved["summary"]["error_count"]}项'
    if saved['status'] == 'no_parameters':
        message = '页面明确显示无参数，已记录空态及原始 HTML'
    return {**saved, 'message': message}
