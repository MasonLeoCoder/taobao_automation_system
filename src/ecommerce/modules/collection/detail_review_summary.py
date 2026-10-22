"""评价概览单独合并进商品 JSON，历史仅保留概览片段。"""
import hashlib
import json
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection.detail_storage import product_directory, save_detail_section
from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.detail_review_summary import collect_review_summary_data, read_review_summary, snapshot_key
from ecommerce.platforms.taobao.verification import active_guard


class _SummaryChanged(Exception):
    """保存前概览或目标商品发生变化。"""


def save_review_summary(directory, record, raw_html, *, attempt=None):
    product_dir = directory.parent.parent
    directory.mkdir(parents=True, exist_ok=True)
    stem = 'review_summary' if attempt is None else f'review_summary-attempt-{attempt}'
    html_path, destination = directory / (stem + '.html'), directory / (stem + '.json')
    saved = {**record, 'html_file': None, 'html_sha256': None, 'html_size_bytes': 0,
             'history_record_file': destination.relative_to(product_dir).as_posix()}
    if raw_html is not None:
        raw_bytes = raw_html.encode('utf-8')
        temporary = html_path.with_suffix('.html.tmp')
        try:
            temporary.write_bytes(raw_bytes)
            temporary.replace(html_path)
        finally:
            temporary.unlink(missing_ok=True)
        saved.update(html_file=html_path.relative_to(product_dir).as_posix(),
                     html_sha256=hashlib.sha256(raw_bytes).hexdigest(), html_size_bytes=len(raw_bytes))
    temporary = destination.with_suffix('.json.tmp')
    try:
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    section = 'review_summary' if saved['status'] == 'complete' else 'review_summary_last_attempt'
    record_path = save_detail_section(product_dir, record['product_id'], section, saved)
    return {**saved, 'record_path': str(record_path.resolve()), 'history_record_path': str(destination.resolve())}


def collect_review_summary(settings, result, *, stable_ms=500):
    guard = active_guard()
    if guard is None:
        raise RuntimeError('评价概览采集必须在淘宝专用守卫中运行')
    item_id = result.target.item_id
    batch_id = guard.task_id + '-' + uuid4().hex[:8]
    directory = product_directory(settings, item_id) / 'history' / batch_id
    base = {'schema_version': 1, 'product_id': item_id, 'task_id': guard.task_id, 'batch_id': batch_id,
            'source_url': result.target.canonical_url, 'scope': 'detail_page_review_overview',
            'individual_reviews_collected': False, 'all_reviews_opened': False}

    def record_for(value, status):
        return {**base, 'captured_at': now(), 'status': status, 'state': value['state'],
                'heading_raw': value.get('heading_raw'), 'review_count': value.get('review_count'),
                'positive_rate': value.get('positive_rate'), 'tags': value['tags'],
                'tags_status': value.get('tags_status', 'not_loaded'), 'tag_count': len(value['tags']),
                'all_reviews_entry_present': value.get('all_reviews_entry_present', False),
                'diagnostic_count': value.get('diagnostic_count', 0), 'reason': value.get('reason'),
                'selector': value.get('selector')}

    def diagnostic(value, attempt):
        if matches_detail_url(result.page, item_id):
            save_review_summary(directory, record_for(value, 'partial'), value.get('raw_html'), attempt=attempt)

    while True:
        value = collect_review_summary_data(result, settings.timeout_ms, on_diagnostic=diagnostic, stable_ms=stable_ms)

        def save():
            try:
                current = read_review_summary(result.page, item_id)
            except (ValueError, PlaywrightError) as error:
                raise _SummaryChanged from error
            if snapshot_key(current) != snapshot_key(value):
                raise _SummaryChanged
            return save_review_summary(directory, record_for(value, 'complete'), value['raw_html'])

        try:
            saved = guard.perform('保存评价概览与概览 HTML 片段', save)
            break
        except _SummaryChanged:
            guard.request_attention(result.page, '保存前评价概览或目标商品发生变化，请确认原商品后回复继续，将重新读取概览', step='复核评价概览保存')
    return {**saved, 'message': f'评价概览采集完成：评价{saved["review_count"]["raw"]}，总结标签{saved["tag_count"]}项'}
