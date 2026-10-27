"""问大家：默认50题，逐题收齐页面可提供的回答，逐批保存并整批发布。"""
from ecommerce.infrastructure.tasks.events import notify as print
from ecommerce.infrastructure.storage.collection_codes import stop_reason_fields
import csv
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

from ecommerce.infrastructure.storage.database import now
from ecommerce.infrastructure.tasks.runner import current_task
from ecommerce.modules.collection.detail_images import _remove_owned_tree
from ecommerce.modules.collection.detail_reviews import atomic_json, compact, csv_text
from ecommerce.modules.collection.detail_storage import product_directory, read_detail_document, save_detail_section
from ecommerce.modules.collection.export import format_capture_time
from ecommerce.platforms.taobao.detail import matches_detail_url
from ecommerce.platforms.taobao.detail_questions import all_questions_selected, ensure_questions_open, question_action, read_questions, scroll_region, snapshot_key, wait_snapshot
from ecommerce.platforms.taobao.detail_review_summary import parse_display_count
from ecommerce.platforms.taobao.verification import active_guard
from ecommerce.shared.exceptions import TaskStopped

log = logging.getLogger(__name__)


def keyed(rows, kind):
    """匿名同文记录用本批出现序号区分；媒体的懒加载地址不参与身份。"""
    seen = Counter()
    fields = ('question', 'question_author', 'question_date_raw') if kind == 'question' else ('author', 'content', 'meta_raw')
    for row in rows:
        digest = hashlib.sha256(compact({k: row.get(k) for k in fields}).encode('utf-8')).hexdigest()
        seen[digest] += 1
        identity = row.get(kind + '_id')
        yield ('id:' + identity if identity else f'fp:{digest}:{seen[digest]}'), row


class QuestionStore:
    """问题与回答分别事务落盘；回答键始终绑定父问题，不保存预览为正式回答。"""
    def __init__(self, directory, item_id):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / 'questions-progress.sqlite3')
        self.db.execute('CREATE TABLE questions (position INTEGER PRIMARY KEY, question_key TEXT UNIQUE, payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE answers (question_key TEXT, position INTEGER, answer_key TEXT, payload TEXT NOT NULL, PRIMARY KEY(question_key,answer_key), UNIQUE(question_key,position))')
        self.db.commit()
        self.item_id = item_id

    @property
    def count(self):
        return self.db.execute('SELECT count(*) FROM questions').fetchone()[0]

    @property
    def answer_count(self):
        return self.db.execute('SELECT count(*) FROM answers').fetchone()[0]

    def get(self, key):
        row = self.db.execute('SELECT payload FROM questions WHERE question_key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def ingest(self, key, row):
        old = self.get(key)
        question = old or {'index': self.count + 1, 'product_id': self.item_id, 'question_key': key,
            'question_id': row['question_id'], 'identity_method': 'question_id' if row['question_id'] else 'fingerprint_occurrence',
            'identity_exact': bool(row['question_id']), 'question': row['question'], 'author': row.get('question_author'),
            'date_raw': row.get('question_date_raw'), 'preview_text': row.get('preview_text'), 'captured_at': now(),
            'answers_complete': False, 'answer_stop_reason': 'not_expanded', 'answer_count': 0}
        with self.db:
            if not old:
                self.db.execute('INSERT INTO questions VALUES (?,?,?)', (question['index'], key, compact(question)))
            if row['expanded']:
                # 已加载的新答案依页面顺序追加；预览不进入此表，也不参与去重。
                for answer_key, answer in keyed(row['answers'], 'answer'):
                    if answer['errors']:
                        break
                    old_answer = self.db.execute('SELECT position,payload FROM answers WHERE question_key=? AND answer_key=?', (key, answer_key)).fetchone()
                    position = old_answer[0] if old_answer else self.db.execute('SELECT count(*) FROM answers WHERE question_key=?', (key,)).fetchone()[0] + 1
                    date_raw, separator, sku = (answer.get('meta_raw') or '').partition(' · ')
                    payload = {k: v for k, v in answer.items() if k not in {'dom_index', 'errors', 'expanders'}}
                    payload.update(index=position, answer_key=answer_key, question_key=key,
                        identity_method='answer_id' if answer['answer_id'] else 'fingerprint_occurrence', identity_exact=bool(answer['answer_id']),
                        date_raw=date_raw or None, sku=sku if separator else None,
                        captured_at=json.loads(old_answer[1])['captured_at'] if old_answer else now())
                    self.db.execute('INSERT OR REPLACE INTO answers VALUES (?,?,?,?)', (key, position, answer_key, compact(payload)))
            question['answer_count'] = self.db.execute('SELECT count(*) FROM answers WHERE question_key=?', (key,)).fetchone()[0]
            question['answer_display_mode'] = row.get('answer_display_mode')
            question['answer_count_display'] = parse_display_count(row.get('answer_count_raw'))
            self.db.execute('UPDATE questions SET payload=? WHERE question_key=?', (compact(question), key))
        return question

    def complete(self, key, reason):
        value = self.get(key)
        value.pop('answer_stop_reason_message',None)
        value.update(answers_complete=True, answer_stop_reason=reason)
        with self.db:
            self.db.execute('UPDATE questions SET payload=? WHERE question_key=?', (compact(value), key))

    def invalidate(self, key, reason):
        value = self.get(key)
        if value:
            value.update(answers_complete=False, **stop_reason_fields(reason,field='answer_stop_reason'))
            with self.db:
                self.db.execute('UPDATE questions SET payload=? WHERE question_key=?', (compact(value), key))

    def close(self):
        self.db.close()


class QuestionPacer:
    """每10题或5分钟休息；扣除人工暂停，长休息通过守卫可随时停止。"""
    def __init__(self, guard):
        self.guard, self.completed, self.rests = guard, 0, 0
        self.started = self.active_time()

    def active_time(self):
        return time.monotonic() - self.guard.paused_seconds

    def before_action(self, *, next_question=False):
        if self.completed >= 10 or self.active_time() - self.started >= 300:
            print('问大家采集中途休息，休息结束后继续；可随时暂停或停止。', flush=True)
            self.guard.pause_between('question_rest')
            self.started, self.completed = self.active_time(), 0
            self.rests += 1
        if next_question:
            self.guard.pause_between('question')


def export_questions(store, directory, record):
    folder = directory / '问大家'
    folder.mkdir(exist_ok=True)
    jp, cp = folder / '问答明细.json', folder / '问答明细.csv'
    jt, ct = jp.with_suffix('.json.tmp'), cp.with_suffix('.csv.tmp')
    columns = ('问题序号', '商品ID', '问题ID', '问题内容', '提问者', '提问日期原文', '回答序号', '回答ID', '回答用户',
               '回答内容', '回答日期原文', '购买规格', '用户身份标签', '购买时间标签', '图片链接', '视频链接', '视频封面链接',
               '媒体说明', '回答采集完整', '回答状态', '采集时间')
    try:
        with jt.open('w', encoding='utf-8') as jf, ct.open('w', encoding='utf-8-sig', newline='') as cf:
            header = {k: v for k, v in record.items() if k != 'files'}
            jf.write(compact(header)[:-1] + ',"questions":[')
            writer = csv.writer(cf)
            writer.writerow(columns)
            for qi, (key, raw) in enumerate(store.db.execute('SELECT question_key,payload FROM questions ORDER BY position')):
                q = json.loads(raw)
                jf.write((',' if qi else '') + '\n' + compact(q)[:-1] + ',"answers":[')
                answers = store.db.execute('SELECT payload FROM answers WHERE question_key=? ORDER BY position', (key,))
                count = 0
                for count, (answer_raw,) in enumerate(answers, 1):
                    a = json.loads(answer_raw)
                    jf.write((',' if count > 1 else '') + '\n' + answer_raw)
                    writer.writerow([q['index'], q['product_id'], csv_text(q['question_id']), csv_text(q['question']), csv_text(q['author']), csv_text(q['date_raw']),
                        a['index'], csv_text(a['answer_id']), csv_text(a['author']), csv_text(a['content']), csv_text(a['date_raw']), csv_text(a['sku']),
                        *[csv_text(json.dumps(a.get(k, []), ensure_ascii=False)) for k in ('identity_labels', 'purchase_labels', 'image_urls', 'video_urls', 'video_poster_urls', 'unresolved_media')],
                        q['answers_complete'], q['answer_stop_reason'], format_capture_time(a['captured_at'])])
                if not count:
                    writer.writerow([q['index'], q['product_id'], csv_text(q['question_id']), csv_text(q['question']), csv_text(q['author']), csv_text(q['date_raw']),
                        *([''] * 12), q['answers_complete'], '暂无回答' if q['answer_stop_reason'] == 'no_answers' else q['answer_stop_reason'], format_capture_time(q['captured_at'])])
                jf.write(']}')
            jf.write('\n]}\n')
        jt.replace(jp)
        ct.replace(cp)
    finally:
        jt.unlink(missing_ok=True)
        ct.unlink(missing_ok=True)
    product_dir = directory.parent.parent
    files = []
    for path in (jp, cp):
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        files.append({'name': path.name, 'file': '问大家/' + path.name, 'history_file': path.relative_to(product_dir).as_posix(),
                      'size_bytes': path.stat().st_size, 'sha256': digest})
    saved = {**record, 'files': files, 'history_record_file': (directory / 'questions.json').relative_to(product_dir).as_posix()}
    atomic_json(directory / 'questions.json', saved)
    return saved


def publish_questions(product_dir, saved):
    """校验旧文件与新副本后发布，商品 JSON 写入失败恢复原目录。"""
    destination = product_dir / '问大家'
    token = uuid4().hex[:8]
    staging, backup = product_dir / ('.questions-new-' + token), product_dir / ('.questions-old-' + token)
    if any(p.resolve().parent != product_dir.resolve() for p in (destination, staging, backup)):
        raise ValueError('问答发布路径超出当前商品目录')
    previous = read_detail_document(product_dir, saved['product_id']).get('questions')
    if destination.exists():
        if not previous or destination.is_symlink() or not destination.is_dir():
            raise ValueError('问大家目录未登记，未覆盖')
        registered = {f['name']: f for f in previous.get('files', [])}
        if {p.name for p in destination.iterdir()} != set(registered):
            raise ValueError('问大家目录含未登记内容，未覆盖')
        for name, metadata in registered.items():
            path = destination / name
            if Path(name).name != name or not path.is_file() or path.is_symlink():
                raise ValueError('已有问答文件路径异常，未覆盖')
            with path.open('rb') as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != metadata['sha256']:
                    raise ValueError('已有问答文件已修改，未覆盖')
    moved = installed = False
    try:
        staging.mkdir()
        for value in saved['files']:
            if Path(value['name']).name != value['name']:
                raise ValueError('问答文件名称异常')
            source = (product_dir / value['history_file']).resolve()
            if product_dir.resolve() not in source.parents:
                raise ValueError('问答历史文件超出商品目录')
            staged = staging / value['name']
            shutil.copyfile(source, staged)
            with staged.open('rb') as stream:
                if staged.stat().st_size != value['size_bytes'] or hashlib.file_digest(stream, 'sha256').hexdigest() != value['sha256']:
                    raise ValueError('问答发布副本校验失败')
        if destination.exists():
            destination.replace(backup)
            moved = True
        staging.replace(destination)
        installed = True
        path = save_detail_section(product_dir, saved['product_id'], 'questions', saved)
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


def confirm_questions_batch(settings, item_id, source_batch_id, *, confirmed_count, note):
    """人工核实实际题数后，校验旧批次并创建确认批次；保留原部分结果，不重新抓取。"""
    if type(confirmed_count) is not int or confirmed_count <= 0 or not isinstance(note, str) or not note.strip():
        raise ValueError('人工确认必须包含正整数实际题数和确认说明')
    if not isinstance(source_batch_id, str) or not re.fullmatch(r'[a-f0-9]{32}-[a-f0-9]{8}', source_batch_id):
        raise ValueError('原问答批次编号不正确')
    product_dir = product_directory(settings, item_id)
    source_dir = product_dir / 'history' / source_batch_id
    if source_dir.is_symlink() or product_dir.resolve() not in source_dir.resolve().parents:
        raise ValueError('原问答批次超出商品目录')
    source = json.loads((source_dir / 'questions.json').read_text(encoding='utf-8'))
    if (source.get('product_id') != item_id or source.get('batch_id') != source_batch_id or source.get('status') != 'partial'
            or source.get('completeness_trusted') is False or source.get('collected_count') != confirmed_count
            or source.get('completed_question_count') != confirmed_count
            or not (source.get('last_attention_reason') or '').startswith('连续3轮问答无新增')):
        raise ValueError('仅允许人工确认因列表结束判定而暂停、且所有已采问题回答完整的批次')
    files = {f['name']: f for f in source.get('files', [])}
    if len(source.get('files', [])) != 2 or set(files) != {'问答明细.json', '问答明细.csv'}:
        raise ValueError('原问答文件清单未确认')
    for metadata in files.values():
        path = product_dir / metadata['history_file']
        if source_dir.resolve() not in path.resolve().parents or path.is_symlink() or not path.is_file():
            raise ValueError('原问答明细路径异常')
        with path.open('rb') as stream:
            if path.stat().st_size != metadata['size_bytes'] or hashlib.file_digest(stream, 'sha256').hexdigest() != metadata['sha256']:
                raise ValueError('原问答明细已修改，不能直接发布')
    detail = json.loads((product_dir / files['问答明细.json']['history_file']).read_text(encoding='utf-8'))
    questions = detail['questions']
    if len(questions) != confirmed_count or not all(q['answers_complete'] for q in questions):
        raise ValueError('原问答明细仍有未完成问题')
    database_path = source_dir / 'questions-progress.sqlite3'
    if database_path.is_symlink() or not database_path.is_file():
        raise ValueError('原问答逐批数据库缺失')
    owner = current_task()
    task_id = owner[1] if owner else uuid4().hex
    batch_id = task_id + '-' + uuid4().hex[:8]
    directory = product_dir / 'history' / batch_id
    source_db = sqlite3.connect(database_path.resolve().as_uri() + '?mode=ro', uri=True)
    store = None
    try:
        # 先核对数据库与已校验明细，避免将不对应的进度数据库当成同一批数据。
        actual = []
        for key, raw in source_db.execute('SELECT question_key,payload FROM questions ORDER BY position'):
            q = json.loads(raw)
            q['answers'] = [json.loads(a[0]) for a in source_db.execute('SELECT payload FROM answers WHERE question_key=? ORDER BY position', (key,))]
            actual.append(q)
        if actual != questions or sum(len(q['answers']) for q in actual) != source['answer_count']:
            raise ValueError('原问答数据库与明细不一致')
        store = QuestionStore(directory, item_id)
        source_db.backup(store.db)
        confirmed = {k: v for k, v in source.items() if k not in {'files', 'history_record_file', 'record_path'}}
        confirmed.update(task_id=task_id, batch_id=batch_id, status='complete', stop_reason='manual_confirmation',
            captured_at=now(), end_confirmed=True, title_count_is_reference_only=True,
            source_batch_id=source_batch_id, source_task_id=source['task_id'],
            source_stop_reason=source['stop_reason'],
            message=f'问大家采集完成：{confirmed_count}个问题、{source["answer_count"]}条回答；实际题数已人工确认',
            completion_evidence={'method': 'manual_confirmation', 'confirmed_question_count': confirmed_count,
                                 'confirmed_at': now(), 'note': note.strip()})
        saved = export_questions(store, directory, confirmed)
        path = publish_questions(product_dir, saved)
        return {**saved, 'record_path': str(path.resolve())}
    finally:
        source_db.close()
        if store:
            store.close()


def at_list_bottom(value):
    geometry = value.get('geometry') or {}
    return (geometry.get('overflow') in ('auto', 'scroll') and geometry.get('height', 0) > 0
            and geometry.get('top', 0) + geometry['height'] >= geometry.get('scroll_height', float('inf')) - 2)


def collect_questions(settings, result, *, all_questions=False, stable_ms=500, max_stalls=3):
    guard = active_guard()
    if guard is None:
        raise RuntimeError('问大家采集必须在淘宝守卫中运行')
    if type(all_questions) is not bool:
        raise ValueError('全部问题参数必须是布尔值')
    # 在创建问答批次前完成入口寻找，跳过不能产生零条成功结果或覆盖旧资料。
    while True:
        guard.checkpoint('打开并核对问大家')
        try:
            entry=ensure_questions_open(result,settings.timeout_ms)
            break
        except ValueError as error:
            guard.request_attention(result.page,str(error)+'；请恢复当前商品后回复继续',step='复核问大家入口')
    if entry and entry.get('status')=='skipped':
        from ecommerce.infrastructure.tasks.events import emit_event
        emit_event('collection_module_skipped',entry['message'],details={
            'module':'questions','product_id':result.target.item_id,'reason':'entry_not_found','entry_search':entry['entry_search']})
        log.info(entry['message'])
        return {**entry,'product_id':result.target.item_id,'task_id':guard.task_id,
                'warnings':[{'code':'questions_entry_not_found','message':entry['message']}]}
    item_id, limit = result.target.item_id, None if all_questions else 50
    product_dir = product_directory(settings, item_id)
    batch_id = guard.task_id + '-' + uuid4().hex[:8]
    from ecommerce.modules.collection.recovery import register_attempt
    register_attempt(settings,result.target.item_id,'questions',batch_id,guard.task_id)
    directory = product_dir / 'history' / batch_id
    store, pacer = QuestionStore(directory, item_id), QuestionPacer(guard)
    current, expected = {}, None
    selection_confirmed = False
    last_attention_reason = None
    scrolls = diagnostics = stalls = bottom_rounds = 0
    attempts = set()
    base = {'schema_version': 1, 'product_id': item_id, 'task_id': guard.task_id, 'batch_id': batch_id,
            'source_url': result.target.canonical_url, 'scope': 'page_available_questions_and_answers',
            'mode': 'all' if all_questions else 'limited', 'requested_limit': limit, 'answer_limit': None,
            'media_downloaded': False, 'short_answers_filtered': False, 'started_at': now(),
            'identity_note': '无页面ID时使用文字、显示用户、原文时间及同批出现序号，回答身份绑定父问题；可变媒体URL不参与身份，匿名同文记录身份无法精确确认'}
    previous_resume = guard.resume_check

    def same_selection():
        if not matches_detail_url(result.page, item_id):
            return False
        v = read_questions(result.page, item_id)
        ready = not selection_confirmed or v['state'] == 'ready' and (v.get('panel_present') or v.get('empty_text'))
        return ready and all_questions_selected(v) and (expected is None or any(k == expected for k, _ in keyed(v['rows'], 'question')))

    guard.resume_check = same_selection

    def record(status, reason, *, end=False):
        completed = sum(json.loads(r[0])['answers_complete'] for r in store.db.execute('SELECT payload FROM questions'))
        display_count = parse_display_count(current.get('count_raw'))
        return {**base, 'status': status, **stop_reason_fields(reason), 'captured_at': now(), 'collected_count': store.count,
                'answer_count': store.answer_count, 'completed_question_count': completed, 'end_confirmed': end,
                'question_count_display': display_count, 'question_count_difference': None if display_count['value'] is None else display_count['value'] - store.count,
                'last_attention_reason': last_attention_reason, 'filter': '全部', 'title_count_is_reference_only': True,
                'bottom_confirmation_rounds': bottom_rounds,
                'completion_evidence': {'method': reason, 'bottom_rounds': bottom_rounds} if end else None,
                'scroll_count': scrolls, 'rest_count': pacer.rests, 'diagnostic_count': diagnostics,
                'empty_text': current.get('empty_text'), 'end_text': current.get('end_text'),
                'message': '无问答' if status == 'no_questions' else f'已采集{store.count}个问题、{store.answer_count}条回答；结束原因：{reason}'}

    def progress():
        value=record('running','collecting')
        atomic_json(directory / 'questions-progress.json', value)
        from ecommerce.modules.collection.persistence import capture_progress
        capture_progress(settings,'questions',value,directory/'questions-progress.json')
        guard.update_progress(collected=store.count, target=limit)

    def pause(reason):
        nonlocal diagnostics, stalls, last_attention_reason
        diagnostics += 1
        last_attention_reason = reason
        partial = export_questions(store, directory, record('partial', reason))
        save_detail_section(product_dir, item_id, 'questions_last_attempt', partial)
        guard.request_attention(result.page, reason + '；已保存本批进度。请保留当前问题、恢复全部筛选并处理后回复继续', step='复核问大家采集')
        stalls = 0

    def snapshot():
        return wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms)

    def click(row, control, action):
        # 同一状态、同一入口只点一次；明确数据变化后才可使用下一批“更多”。
        # 滚动/浏览器注入节点可能改变绝对DOM路径；路径不作为“全部/更多”动作的身份。
        slot = control['path'] if action == '展开问题或回答全文' else None
        action_key = (expected, action, slot, control['text'], hashlib.sha256(compact([(a['author'], a['content'], a['meta_raw']) for a in row['answers']]).encode()).hexdigest())
        if action_key in attempts:
            pause('同一回答入口仍未完成，已阻止重复点击')
            return
        pacer.before_action()
        attempts.add(action_key)
        try:
            question_action(result, row, control, settings.timeout_ms, step=action)
        except ValueError as error:
            pause(str(error))

    def scroll(geometry, question=None):
        nonlocal scrolls
        pacer.before_action()
        try:
            scroll_region(result, geometry, question=question)
            scrolls += 1
        except ValueError as error:
            pause(str(error))

    try:
        current = snapshot()
        selection_confirmed = True
        while True:
            guard.checkpoint('读取并保存问大家')
            if current.get('state') != 'ready' or not current.get('stable') or not same_selection():
                pause(current.get('reason') or '当前问题、筛选或列表加载状态未通过复核')
                current = snapshot()
                continue
            pending = [(k, r) for k, r in keyed(current['rows'], 'question')
                       if not (store.get(k) or {}).get('answers_complete') or r['more_controls'] or r['errors']]
            if pending:
                bottom_rounds = 0
                key, row = pending[0]
                if (store.get(key) or {}).get('answers_complete'):
                    store.invalidate(key, 'additional_content_pending')
                if expected != key:
                    pacer.before_action(next_question=True)
                    expected = key
                    # 间隔期间可能发生人工操作，随后重新读取并匹配当前问题。
                    current = snapshot()
                    continue
                if row['errors'] or row['loading']:
                    pause('；'.join(row['errors']) or '当前问题仍在加载')
                    current = snapshot()
                    continue
                controls = row['expanders'] + [c for a in row['answers'] for c in a['expanders']]
                if controls:
                    click(row, controls[0], '展开问题或回答全文')
                    current = snapshot()
                    continue
                q = store.ingest(key, row)
                progress()
                errors = [e for a in row['answers'] for e in a['errors']]
                if errors:
                    pause('；'.join(errors))
                    current = snapshot()
                    continue
                declared = parse_display_count(row.get('answer_count_raw'))['value']
                if declared is not None and q['answer_count'] > declared:
                    pause('已采集回答数超过页面明确数量，请核对问题身份及页面变化')
                    current = snapshot()
                    continue
                complete_reason = ('no_answers' if row['answer_empty_text'] and not q['answer_count'] else
                    'explicit_end' if row['answer_end_text'] and row['expanded'] else
                    'exact_answer_count' if declared is not None and declared == q['answer_count'] and row['expanded'] else
                    ('fully_visible_without_toggle' if row.get('answer_display_mode') == 'full_without_toggle' else 'expanded_all_visible')
                        if row['answers_complete'] and (declared is None or declared == q['answer_count']) else None)
                if complete_reason:
                    store.complete(key, complete_reason)
                    pacer.completed += 1
                    print(f'问大家进度：第{q["index"]}题完成，{q["answer_count"]}条回答；累计{store.answer_count}条回答', flush=True)
                    progress()
                    expected = None
                    stalls = 0
                    if limit is not None and store.count >= limit:
                        final = record('complete', 'limit_reached')
                        break
                    current = snapshot()
                    continue
                before = snapshot_key({k: v for k, v in current.items() if k != 'stable'})
                if not row['expanded'] and row['open_controls']:
                    click(row, row['open_controls'][0], '展开当前问题的全部回答')
                elif row['more_controls']:
                    guard.pause_between('page')
                    click(row, row['more_controls'][0], '加载当前问题的更多回答')
                elif row['expanded'] and row['answer_geometry'] and row['answer_geometry']['overflow'] in ('auto', 'scroll'):
                    scroll(row['answer_geometry'], row)
                else:
                    pause('回答尚未确认完整，且没有已识别的展开或加载入口')
                    current = snapshot()
                    continue
            else:
                expected = None
                if current.get('empty_text') and not store.count:
                    final = record('no_questions', 'no_questions', end=True)
                    log.info('商品 %s：无问答', item_id)
                    print(f'商品 {item_id}：无问答', flush=True)
                    break
                if current.get('end_text'):
                    final = record('complete', 'end_of_list', end=True)
                    break
                if not current.get('geometry'):
                    pause('问大家滚动区域尚未确认')
                    current = snapshot()
                    continue
                before = snapshot_key({k: v for k, v in current.items() if k != 'stable'})
                scroll(current['geometry'])
            # 列表底部每轮至少等待内容稳定及加载预算，标题数只留作参考。
            waiting_for_end = expected is None and at_list_bottom(current)
            wait_budget = min(settings.timeout_ms, 5000) if waiting_for_end else settings.timeout_ms
            current = wait_snapshot(result, wait_budget, stable_ms=stable_ms, changed_from=before)
            if waiting_for_end and current.get('state') != 'ready':
                current = wait_snapshot(result, settings.timeout_ms, stable_ms=stable_ms, changed_from=before)
            if snapshot_key({k: v for k, v in current.items() if k != 'stable'}) == before:
                stalls += 1
                if waiting_for_end and current.get('state') == 'ready' and current.get('stable') and at_list_bottom(current):
                    bottom_rounds += 1
                    if bottom_rounds >= max_stalls:
                        final = record('complete', 'stable_bottom', end=True)
                        break
                else:
                    bottom_rounds = 0
                if stalls >= max_stalls:
                    pause('连续3轮问答无新增，尚未确认末尾')
                    current = snapshot()
            else:
                stalls = bottom_rounds = 0
        saved = export_questions(store, directory, final)
        def publish():
            if not same_selection():
                raise ValueError('发布前问大家商品或筛选发生变化')
            return publish_questions(product_dir, saved)
        path = guard.perform('发布问大家明细与商品记录', publish)
        (directory / 'questions-progress.json').unlink(missing_ok=True)
        return {**saved, 'record_path': str(path.resolve())}
    except BaseException as error:
        reason = 'user_stopped' if isinstance(error, (TaskStopped, KeyboardInterrupt)) else 'collection_error'
        try:
            partial = export_questions(store, directory, record('partial', reason))
            save_detail_section(product_dir, item_id, 'questions_last_attempt', partial)
        except Exception:
            log.error('问大家中断记录未能完整写入，逐批数据库保留于 %s', directory)
        raise
    finally:
        store.close()
        guard.resume_check = previous_resume
