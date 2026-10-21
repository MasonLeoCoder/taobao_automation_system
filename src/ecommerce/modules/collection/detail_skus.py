"""保存商品基础信息、SKU 价格和按规格名称命名的原图片。"""
import hashlib
import json
import re
import shutil
import unicodedata
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from uuid import uuid4

from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection.detail_images import _remove_owned_tree, download_image
from ecommerce.modules.collection.detail_storage import product_directory, read_detail_document, save_detail_sections
from ecommerce.platforms.taobao.detail import matches_detail_url, resolve_detail_input
from ecommerce.platforms.taobao.detail_skus import collect_sku_data
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention

SKU_IMAGE_FOLDER = 'sku图'
RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$', 'CONIN$', 'CONOUT$',
                  *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


def filename_stem(name, identity, used):
    """Windows 文件名清理；保留原 SKU 名称于 JSON，并阻止大小写等冲突。"""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', unicodedata.normalize('NFC', name)).strip().rstrip('. ')
    stem = stem or '未命名规格'
    if stem.split('.')[0].upper() in RESERVED_NAMES:
        stem = 'SKU_' + stem
    while len(stem.encode('utf-16-le')) > 192:
        stem = stem[:-1]
    stem = stem.rstrip('. ')
    key = stem.casefold()
    if key in used:
        suffix = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:10]
        stem = stem[:78].rstrip('. ') + '_' + suffix
        counter = 2
        candidate = stem
        while candidate.casefold() in used:
            candidate = stem + '_' + str(counter)
            counter += 1
        stem = candidate
    used.add(stem.casefold())
    return stem


def _check_owned(path, product_dir):
    resolved = path.resolve()
    resolved.relative_to(product_dir.resolve())
    if resolved == product_dir.resolve():
        raise ValueError('SKU 图片路径不能是商品根目录')


def save_sku_result(directory, record):
    product_dir = directory.parent.parent
    item_id = record['product_id']
    document = read_detail_document(product_dir, item_id)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / 'sku_details.json'
    temporary = destination.with_suffix('.json.tmp')
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    if record['status'] != 'complete':
        record_path = save_detail_sections(product_dir, item_id, {'sku_details_last_attempt': record})
        return {**record, 'record_path': str(record_path.resolve()), 'history_record_path': str(destination.resolve())}
    target = product_dir / SKU_IMAGE_FOLDER
    if target.exists():
        previous = document.get('sku_details', {}).get('skus', [])
        known = {sku['image']['image_file'].split('/')[-1]: sku['image']['sha256'] for sku in previous if sku.get('image')}
        if set(p.name for p in target.iterdir()) != set(known):
            raise ValueError('现有 SKU 图片目录与登记信息不一致，未覆盖')
        for path in target.iterdir():
            if not path.is_file() or path.is_symlink():
                raise ValueError('现有 SKU 图片存在非登记文件，未覆盖')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != known[path.name]:
                    raise ValueError('现有 SKU 图片已修改，未覆盖')
    staging = product_dir / f".sku-images-{record['batch_id']}.tmp"
    backup = product_dir / f".sku-images-{record['batch_id']}.backup"
    for path in (target, staging, backup):
        _check_owned(path, product_dir)
    latest = {**record, 'folder': SKU_IMAGE_FOLDER,
              'history_record_file': destination.relative_to(product_dir).as_posix(),
              'skus': [{**sku, 'image': {**sku['image'], 'image_path': str((target / sku['image']['image_file'].split('/')[-1]).resolve())}
                       if sku.get('image') else None} for sku in record['skus']]}
    installed = False
    try:
        shutil.copytree(directory / SKU_IMAGE_FOLDER, staging)
        if target.exists():
            target.replace(backup)
        staging.replace(target)
        installed = True
        sku_section = {k: v for k, v in latest.items() if k != 'product_info'}
        record_path = save_detail_sections(product_dir, item_id, {'product_info': latest['product_info'], 'sku_details': sku_section})
    except Exception:
        if installed:
            _remove_owned_tree(target, product_dir)
        if backup.exists():
            backup.replace(target)
        raise
    finally:
        _remove_owned_tree(staging, product_dir)
    _remove_owned_tree(backup, product_dir)
    return {**latest, 'record_path': str(record_path.resolve()), 'history_record_path': str(destination.resolve())}


def collect_skus(settings, result, *, settle_ms=1200):
    guard = active_guard()
    if guard is None:
        raise RuntimeError('SKU 采集必须在淘宝专用守卫中运行')
    batch_id = guard.task_id + '-' + uuid4().hex[:8]
    directory = product_directory(settings, result.target.item_id) / 'history' / batch_id
    (directory / SKU_IMAGE_FOLDER).mkdir(parents=True, exist_ok=True)
    progress_path = directory / 'sku_progress.json'
    def save_progress(data):
        if not matches_detail_url(result.page, result.target.item_id):
            raise NeedsAttention('保存 SKU 进度前商品发生变化')
        progress = {**data, 'product_id': result.target.item_id, 'batch_id': batch_id, 'task_id': guard.task_id,
                    'status': 'in_progress', 'updated_at': now()}
        temporary = progress_path.with_suffix('.json.tmp')
        try:
            temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(progress_path)
        finally:
            temporary.unlink(missing_ok=True)
    data = collect_sku_data(result, settings.timeout_ms, settle_ms=settle_ms, on_progress=save_progress)
    record = {**data, 'schema_version': 1, 'batch_id': batch_id, 'task_id': guard.task_id,
              'product_id': result.target.item_id, 'source_url': resolve_detail_input(result.page.url).canonical_url,
              'captured_at': now(), 'scope': 'product_info_and_skus', 'status': 'complete'}
    used = set()
    for sku in record['skus']:
        sku['image'] = None
        sku['image_error'] = None
        if sku['image_status'] == 'no_image':
            continue
        identity = json.dumps(sku['properties'], ensure_ascii=False, sort_keys=True)
        stem = filename_stem(sku['name'], identity, used)
        try:
            image = download_image({'index': sku['index'], 'image_url': sku['image_url']},
                                   directory / SKU_IMAGE_FOLDER, result, guard, file_stem=stem,
                                   image_folder=SKU_IMAGE_FOLDER, label='SKU图片')
            sku.update(image=image, image_status='downloaded')
        except HTTPError as error:
            sku.update(image_status='download_failed', image_error=f'HTTP {error.code}；未自动重试')
            if error.code in {401, 403, 429}:
                guard.request_attention(result.page, f'SKU 图片返回 HTTP {error.code}，请人工检查访问状态后回复继续；本图片不会自动重试', step='检查 SKU 图片访问')
        except (URLError, OSError, ValueError, IncompleteRead) as error:
            sku.update(image_status='download_failed', image_error=f'图片下载或校验失败（{type(error).__name__}）；未自动重试')
    summary = {
        'candidate_count': len(record['skus']),
        'recognized_count': sum(bool(s['combination_confirmed']) for s in record['skus']),
        'price_collected_count': sum(s['price_status'] == 'collected' for s in record['skus']),
        'out_of_stock_count': sum(s['stock_status'] == 'out_of_stock' for s in record['skus']),
        'unavailable_unconfirmed_count': sum(s['stock_status'] == 'unavailable_unconfirmed' for s in record['skus']),
        'image_downloaded_count': sum(s['image_status'] == 'downloaded' for s in record['skus']),
        'image_failed_count': sum(s['image_status'] == 'download_failed' for s in record['skus']),
        'no_image_count': sum(s['image_status'] == 'no_image' for s in record['skus']),
    }
    record['summary'] = summary
    if (summary['unavailable_unconfirmed_count'] or summary['image_failed_count']
            or summary['recognized_count'] != summary['candidate_count']):
        record['status'] = 'partial'
    def save():
        if not matches_detail_url(result.page, result.target.item_id):
            raise NeedsAttention('SKU 保存前目标商品发生变化')
        return save_sku_result(directory, record)
    saved = guard.perform('保存商品信息、SKU 价格及图片', save)
    progress_path.unlink(missing_ok=True)
    return {**saved, 'message': f"SKU采集{('完成' if record['status']=='complete' else '部分完成')}：识别{summary['recognized_count']}项，"
            f"价格{summary['price_collected_count']}项，缺货{summary['out_of_stock_count']}项，图片{summary['image_downloaded_count']}张"}
