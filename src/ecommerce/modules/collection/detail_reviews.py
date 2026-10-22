"""逐批落盘评价，默认100条有效记录；完成后发布 JSON/CSV，异常保留历史。"""
import csv
import hashlib
import json
import logging
import re
import shutil
import sqlite3
from collections import Counter
from datetime import date as calendar_date
from pathlib import Path
from uuid import uuid4

from ecommerce.infrastructure.storage.database import now
from ecommerce.modules.collection.detail_images import _remove_owned_tree
from ecommerce.modules.collection.export import format_capture_time
from ecommerce.modules.collection.detail_storage import product_directory, read_detail_document, save_detail_section
from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.detail_review_summary import parse_display_count, read_review_summary
from ecommerce.platforms.taobao.detail_reviews import ensure_reviews_open, expand_review, read_reviews, resolve_review_video, scroll_reviews, snapshot_key, wait_snapshot
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped

TEMPLATE_REVIEW = '该用户觉得商品非常好，给出好评'
log = logging.getLogger(__name__)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '-' + uuid4().hex[:8] + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(row):
    # 图片占位图/尺寸地址会随滚动变化，不能作为评价身份；同批同文节点用出现序号区分。
    identity = {k: row.get(k) for k in ('author', 'meta_raw', 'content')}
    identity['append_reviews'] = [{k: a.get(k) for k in ('time_raw', 'content')} for a in row.get('append_reviews', [])]
    return hashlib.sha256(compact(identity).encode('utf-8')).hexdigest()


def is_template(row):
    meaningful_append = any(r.get('content') or r.get('image_urls') or r.get('video_urls') or r.get('unresolved_media') for r in row['append_reviews'])
    return (re.sub(r'\s+', '', row.get('content') or '') == re.sub(r'\s+', '', TEMPLATE_REVIEW)
            and not meaningful_append and not row['image_urls'] and not row['video_urls'] and not row['unresolved_media'])


def review_record(row, key, index, item_id):
    meta = row.get('meta_raw') or ''
    split = re.split(r'已购\s*[:：]\s*', meta, maxsplit=1)
    date_raw, sku = split[0].strip() or None, split[1].strip() if len(split) == 2 else None
    match = re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date_raw or '')
    try:
        date = calendar_date(int(match[1]), int(match[2]), int(match[3])).isoformat() if match else None
    except ValueError:
        date = None
    return {'index': index, 'product_id': item_id, 'review_key': key, 'review_id': row['review_id'],
            'identity_method': 'review_id' if row['review_id'] else 'fingerprint_occurrence',
            'identity_exact': bool(row['review_id']), 'author': row['author'], 'date_raw': date_raw, 'date': date,
            'sku': sku, 'meta_raw': meta, 'content': row['content'], 'image_urls': row['image_urls'],
            'video_urls': row['video_urls'], 'video_poster_urls': row.get('video_poster_urls', []), 'unresolved_media': row['unresolved_media'],
            'append_reviews': [{k: v for k, v in a.items() if k not in {'body_count', 'unparsed', 'pending_media', 'video_tiles'}} for a in row['append_reviews']],
            'captured_at': now()}


class ReviewStore:
    """SQLite 保证逐批事务和低内存导出；记录ID或指纹出现序号去除重叠加载。"""
    def __init__(self, directory, item_id, limit):
        directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(directory / 'reviews-progress.sqlite3')
        self.connection.execute('CREATE TABLE seen (review_key TEXT PRIMARY KEY, skipped INTEGER NOT NULL)')
        self.connection.execute('CREATE TABLE reviews (position INTEGER PRIMARY KEY, review_key TEXT UNIQUE, payload TEXT NOT NULL)')
        self.connection.commit()
        self.item_id, self.limit, self.collected, self.skipped, self.scanned = item_id, limit, 0, 0, 0

    def ingest(self, rows):
        occurrences = Counter()
        error, pending = None, False
        added_before, skipped_before = self.collected, self.skipped
        collected, skipped_count, scanned = self.collected, self.skipped, self.scanned
        with self.connection:
            for row in rows:
                if self.limit is not None and collected >= self.limit:
                    break
                if row['errors']:
                    error = '；'.join(row['errors'])
                    break
                digest = fingerprint(row)
                occurrences[digest] += 1
                key = 'id:' + row['review_id'] if row['review_id'] else f'fp:{digest}:{occurrences[digest]}'
                existing = self.connection.execute('SELECT skipped FROM seen WHERE review_key=?', (key,)).fetchone()
                if existing:
                    if not existing[0] and not row.get('pending_media'):
                        position, payload = self.connection.execute('SELECT position,payload FROM reviews WHERE review_key=?', (key,)).fetchone()
                        old = json.loads(payload)
                        updated = review_record(row, key, position, self.item_id)
                        updated['captured_at'] = old['captured_at']
                        self.connection.execute('UPDATE reviews SET payload=? WHERE review_key=?', (compact(updated), key))
                    continue
                if row.get('pending_media'):
                    pending = True
                    break  # 先加载本条再处理后续，维持页面顺序，不能保存占位图地址。
                skipped = is_template(row)
                self.connection.execute('INSERT INTO seen VALUES (?,?)', (key, int(skipped)))
                scanned += 1
                if skipped:
                    skipped_count += 1
                    continue
                collected += 1
                value = review_record(row, key, collected, self.item_id)
                self.connection.execute('INSERT INTO reviews VALUES (?,?,?)', (collected, key, compact(value)))
        self.collected, self.skipped, self.scanned = collected, skipped_count, scanned
        return self.collected-added_before, self.skipped-skipped_before, error, pending

    def close(self):
        self.connection.close()


def csv_text(value):
    """CSV 单元格按文字显示；JSON 保留原评价。"""
    value = '' if value is None else str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def export_reviews(store, directory, record):
    folder = directory / '用户评价'
    folder.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = folder / '评价明细.json', folder / '评价明细.csv'
    json_temp, csv_temp = json_path.with_suffix('.json.tmp'), csv_path.with_suffix('.csv.tmp')
    fields = ('序号', '商品ID', '用户显示名称', '评价日期原文', '购买规格', '评价正文', '评价图片链接', '评价视频链接', '视频封面链接', '追加评价', '未展示媒体链接说明', '采集时间')
    summary = {k: record.get(k) for k in ('status', 'stop_reason', 'scope', 'requested_limit', 'scanned_count',
        'collected_count', 'skipped_template_count', 'review_count_display', 'folded_count', 'folded_text',
        'end_confirmed', 'bottom_confirmation_rounds', 'completion_evidence')}
    try:
        with json_temp.open('w', encoding='utf-8') as jf, csv_temp.open('w', encoding='utf-8-sig', newline='') as cf:
            jf.write('{"product_id":' + json.dumps(record['product_id']) + ',"batch_id":' + json.dumps(record['batch_id'])
                + ',"summary":' + json.dumps(summary, ensure_ascii=False) + ',"reviews":[')
            writer = csv.writer(cf)
            writer.writerow(fields)
            for position, (payload,) in enumerate(store.connection.execute('SELECT payload FROM reviews ORDER BY position')):
                value = json.loads(payload)
                jf.write((',' if position else '') + '\n' + payload)
                writer.writerow([value['index'], value['product_id'], *map(csv_text, (value['author'], value['date_raw'], value['sku'], value['content'])),
                    csv_text(json.dumps(value['image_urls'], ensure_ascii=False)), csv_text(json.dumps(value['video_urls'], ensure_ascii=False)),
                    csv_text(json.dumps(value.get('video_poster_urls', []), ensure_ascii=False)),
                    csv_text(json.dumps(value['append_reviews'], ensure_ascii=False)), csv_text(json.dumps(value['unresolved_media'], ensure_ascii=False)), format_capture_time(value['captured_at'])])
            jf.write('\n]}\n')
        json_temp.replace(json_path)
        csv_temp.replace(csv_path)
    finally:
        json_temp.unlink(missing_ok=True)
        csv_temp.unlink(missing_ok=True)
    files = []
    product_dir = directory.parent.parent
    for path in (json_path, csv_path):
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        files.append({'name': path.name, 'file': '用户评价/' + path.name, 'history_file': path.relative_to(product_dir).as_posix(),
                      'size_bytes': path.stat().st_size, 'sha256': digest})
    saved = {**record, 'files': files, 'history_record_file': (directory / 'reviews.json').relative_to(product_dir).as_posix()}
    atomic_json(directory / 'reviews.json', saved)
    return saved


def publish_reviews(product_dir, saved):
    """整体发布两份明细；保存商品JSON失败时恢复原文件夹。"""
    destination = product_dir / '用户评价'
    token = uuid4().hex[:8]
    staging, backup = product_dir / ('.reviews-new-' + token), product_dir / ('.reviews-old-' + token)
    resolved_product = product_dir.resolve()
    if any(path.resolve().parent != resolved_product for path in (destination, staging, backup)):
        raise ValueError('评价发布路径超出当前商品目录，未移动文件')
    previous = read_detail_document(product_dir, saved['product_id']).get('reviews')
    if destination.exists():
        if not previous or not destination.is_dir() or destination.is_symlink():
            raise ValueError('用户评价目录未登记，未覆盖')
        registered = {f['name']: f for f in previous.get('files', [])}
        if {p.name for p in destination.iterdir()} != set(registered):
            raise ValueError('用户评价目录含未登记内容，未覆盖')
        for name, metadata in registered.items():
            path = destination / name
            if Path(name).name != name or not path.is_file() or path.is_symlink():
                raise ValueError('已有评价文件路径异常，未覆盖')
            with path.open('rb') as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != metadata['sha256']:
                    raise ValueError('已有评价文件已被修改，未覆盖')
    moved, installed = False, False
    try:
        staging.mkdir()
        for value in saved['files']:
            staged = staging / value['name']
            shutil.copyfile(product_dir / value['history_file'], staged)
            with staged.open('rb') as source:
                if staged.stat().st_size != value['size_bytes'] or hashlib.file_digest(source, 'sha256').hexdigest() != value['sha256']:
                    raise ValueError('评价明细发布副本校验失败，未覆盖旧文件')
        if destination.exists():
            destination.replace(backup)
            moved = True
        staging.replace(destination)
        installed = True
        path = save_detail_section(product_dir, saved['product_id'], 'reviews', saved)
    except BaseException:
        if installed:
            _remove_owned_tree(destination, product_dir)
        if moved:
            backup.replace(destination)
        raise
    finally:
        if staging.exists():
            _remove_owned_tree(staging, product_dir)
    if moved:
        _remove_owned_tree(backup, product_dir)
    return path


def folded_bottom_ready(value):
    """已识别无展开入口的折叠尾部，仍须业务层多轮复核无新增。"""
    geometry = value.get('geometry') or {}
    return (value.get('state') == 'ready' and value.get('stable') and value.get('folded_end_candidate')
        and bool(value.get('rows')) and not value.get('expanders')
        and all(not row['errors'] and not row.get('pending_media') for row in value['rows'])
        and geometry.get('overflow') in ('auto', 'scroll') and geometry.get('height', 0) > 0
        and geometry.get('top', 0) + geometry['height'] >= geometry.get('scroll_height', float('inf')) - 2)


def collect_reviews(settings, result, *, all_reviews=False, stable_ms=500, max_stalls=3):
    guard = active_guard()
    if guard is None:
        raise RuntimeError('评价采集必须在淘宝守卫中运行')
    if type(all_reviews) is not bool:
        raise ValueError('全部评价参数必须是布尔值')
    item_id, limit = result.target.item_id, None if all_reviews else 100
    product_dir = product_directory(settings, item_id)
    batch_id = guard.task_id + '-' + uuid4().hex[:8]
    directory = product_dir / 'history' / batch_id
    store = ReviewStore(directory, item_id, limit)
    base = {'schema_version': 1, 'product_id': item_id, 'task_id': guard.task_id, 'batch_id': batch_id,
            'source_url': result.target.canonical_url, 'scope': 'page_available_reviews', 'mode': 'all' if all_reviews else 'limited',
            'requested_limit': limit, 'merchant_replies_collected': False, 'media_downloaded': False,
            'template_filter': TEMPLATE_REVIEW, 'started_at': now()}
    selection, current, zero_without_panel = None, {}, False
    scrolls, diagnostics, stalls, bottom_rounds = 0, 0, 0, 0
    expanded = set()
    video_cache = {}
    previous_resume = guard.resume_check

    def same_selection():
        if not matches_detail_url(result.page, item_id):
            return False
        if selection is None:
            return True
        if zero_without_panel:
            value = read_review_summary(result.page, item_id)
            return value['state'] == 'ready' and value['review_count']['value'] == 0 and not value['all_reviews_entry_present']
        v = read_reviews(result.page, item_id)
        return (v.get('filter'), v.get('sort')) == selection

    guard.resume_check = same_selection

    def record(status, reason, *, end=False):
        folded_count = parse_display_count(current.get('folded_count_raw'))['value']
        return {**base, 'captured_at': now(), 'status': status, 'stop_reason': reason, 'filter': selection[0] if selection else current.get('filter'),
                'sort': selection[1] if selection else current.get('sort'), 'scanned_count': store.scanned, 'collected_count': store.collected,
                'skipped_template_count': store.skipped, 'end_confirmed': end,
                'review_availability': 'none' if status == 'no_reviews' else 'present' if store.scanned else 'unconfirmed',
                'all_filtered': store.scanned > 0 and store.collected == 0 and store.skipped == store.scanned,
                'scroll_count': scrolls, 'diagnostic_count': diagnostics, 'folded_text': current.get('folded_text'),
                'folded_count': folded_count, 'review_count_display': parse_display_count(current.get('count_raw')),
                'bottom_confirmation_rounds': bottom_rounds,
                'completion_evidence': {'method': reason, 'bottom_rounds': bottom_rounds, 'folded_text': current.get('folded_text')}
                    if reason == 'folded_visible_end' else None,
                'empty_text': current.get('empty_text'), 'end_text': current.get('end_text'),
                'identity_note': '无页面评价ID时使用用户、时间、规格、正文、追评文字及同批出现序号去重，标记identity_exact=false；可变媒体链接不参与身份，无法保证不可区分匿名记录的精确身份',
                'message': '无评价' if status == 'no_reviews' else
                    f'当前页面可见评价采集完成：已采集{store.collected}条有效评价，跳过模板评价{store.skipped}条；平台提示折叠{folded_count}条评价'
                    if reason == 'folded_visible_end' else f'已采集{store.collected}条有效评价，跳过模板评价{store.skipped}条；结束原因：{reason}'}

    def progress(status='running', reason='collecting'):
        atomic_json(directory / 'reviews-progress.json', record(status, reason))
        guard.update_progress(collected=store.collected, target=limit)

    def pause(reason):
        nonlocal diagnostics, stalls, bottom_rounds
        diagnostics += 1
        progress('partial', reason)
        partial = export_reviews(store, directory, record('partial', reason))
        save_detail_section(product_dir, item_id, 'reviews_last_attempt', partial)
        guard.request_attention(result.page, reason + '；本批已保存，请处理后回复继续。筛选须恢复为全部及原排序', step='复核逐条评价采集')
        stalls = bottom_rounds = 0

    def enrich_videos(value):
        occurrences, new_count = Counter(), 0
        for row in value['rows']:
            digest = fingerprint(row)
            occurrences[digest] += 1
            key = 'id:' + row['review_id'] if row['review_id'] else f'fp:{digest}:{occurrences[digest]}'
            existing = store.connection.execute('SELECT 1 FROM seen WHERE review_key=?', (key,)).fetchone()
            if row.get('pending_media') and not existing or row['errors']:
                break
            if not existing and limit is not None and store.collected + new_count >= limit:
                break
            areas = [row, *row['append_reviews']]
            for area_index, area in enumerate(areas):
                urls = []
                tiles = area.get('video_tiles', [])
                for tile_index, tile in enumerate(tiles):
                    cache_key = (key, area_index, tile_index)
                    if cache_key not in video_cache:
                        video_cache[cache_key] = resolve_review_video(result, row, tile, settings.timeout_ms)
                    urls.append(video_cache[cache_key])
                if urls:
                    area['video_urls'] = list(dict.fromkeys([*area['video_urls'], *urls]))
                    area['unresolved_media'] = [t for t in area['unresolved_media'] if '视频仅展示封面' not in t]
            if not existing and not is_template(row):
                new_count += 1

    try:
        while not matches_detail_url(result.page, item_id):
            pause('评价采集目标商品发生变化')
        current = ensure_reviews_open(result, settings.timeout_ms) or wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
        zero_without_panel = bool(current.get('empty_text') and current.get('geometry') is None and current.get('count_raw') == '0')
        while True:
            guard.checkpoint('读取并保存评价')
            if not matches_detail_url(result.page, item_id):
                pause('评价采集目标商品发生变化')
                current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
                continue
            if current.get('state') != 'ready' or not current.get('stable'):
                pause(current.get('reason') or '评价列表未加载完成或尚未稳定')
                current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
                continue
            pair = (current.get('filter'), current.get('sort'))
            if pair[0] != '全部' or selection is not None and pair != selection:
                pause('评价筛选或排序发生变化')
                current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
                continue
            selection = pair
            # 比较原始DOM状态，避免播放器解析补入的链接导致每轮都被误判为页面变化。
            before_key = snapshot_key(current)
            controls = current.get('expanders', [])
            if controls:
                control = controls[0]
                control_key = (fingerprint(current['rows'][control['row_index']]), control['text'])
                if control_key in expanded:
                    pause('同一全文展开入口仍未完成，已阻止重复点击')
                else:
                    expanded.add(control_key)
                    expand_review(result, control, settings.timeout_ms)
                current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
                continue
            try:
                enrich_videos(current)
            except ValueError as error:
                pause(str(error))
                current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
                continue
            added, skipped, error, pending = store.ingest(current['rows'])
            progress()
            if added or skipped:
                print(f'评价进度：有效{store.collected}条，跳过模板{store.skipped}条，已检查{store.scanned}条', flush=True)
            if error:
                pause(error)
                current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
                continue
            if limit is not None and store.collected >= limit:
                final = record('complete', 'limit_reached')
                break
            if current.get('empty_text'):
                if store.scanned:
                    pause('已经采到评价后出现空态，请核对筛选及页面')
                    current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
                    continue
                final = record('no_reviews', 'no_reviews', end=True)
                log.info('商品 %s：无评价', item_id)
                print(f'商品 {item_id}：无评价', flush=True)
                break
            if current.get('end_text') and not pending:
                final = record('complete', 'end_of_list', end=True)
                break
            before = current
            try:
                scroll_reviews(result, before, settings.timeout_ms)
            except ValueError:
                pause('滚动前目标商品、筛选或排序发生变化')
                current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
                continue
            scrolls += 1
            current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
            if snapshot_key(current) == before_key:
                check_folded_end = not pending and folded_bottom_ready(before)
                wait_budget = min(settings.timeout_ms, 5000) if check_folded_end else settings.timeout_ms
                current = wait_snapshot(result, wait_budget, stable_ms=stable_ms, changed_from=before_key)
                if check_folded_end and current.get('state') != 'ready':
                    current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms, changed_from=before_key)
                if snapshot_key(current) == before_key:
                    stalls += 1
                    if check_folded_end and folded_bottom_ready(current):
                        bottom_rounds += 1
                        if bottom_rounds >= max_stalls:
                            final = record('complete', 'folded_visible_end', end=True)
                            break
                    else:
                        bottom_rounds = 0
                    if stalls >= max_stalls:
                        pause('连续3轮评价列表无新增且滚动位置与高度不变，尚未确认末尾')
                        current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)
                else:
                    stalls = bottom_rounds = 0
            else:
                stalls = bottom_rounds = 0
            if scrolls and scrolls % 500 == 0:
                pause('本批已执行500次滚动，请人工核对后继续；没有截断评价数量')
        saved = export_reviews(store, directory, final)
        def publish():
            if not same_selection():
                raise ValueError('发布前商品、评价筛选或排序发生变化')
            return publish_reviews(product_dir, saved)
        while True:
            try:
                record_path = guard.perform('发布评价明细与商品记录', publish)
                break
            except ValueError as error:
                if same_selection():
                    raise
                pause(str(error))
        (directory / 'reviews-progress.json').unlink(missing_ok=True)
        return {**saved, 'record_path': str(record_path.resolve())}
    except BaseException as error:
        reason = 'user_stopped' if isinstance(error, (TaskStopped, KeyboardInterrupt)) else 'collection_error'
        try:
            progress('partial', reason)
            partial = export_reviews(store, directory, record('partial', reason))
            save_detail_section(product_dir, item_id, 'reviews_last_attempt', partial)
        except Exception:
            log.error('评价中断记录未能完整写入，已有逐批数据库保留于 %s', directory)
        raise
    finally:
        store.close()
        guard.resume_check = previous_resume
