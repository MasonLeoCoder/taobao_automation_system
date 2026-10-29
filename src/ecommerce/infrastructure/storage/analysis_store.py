"""通用分析五表的事务存取；只读采集来源，SQL错误隐藏参数。"""
from contextlib import contextmanager
from uuid import uuid4
import sqlalchemy as sa
from ecommerce.infrastructure.storage.collection_store import collection_store
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now


@contextmanager
def connection(settings, write=False):
    """建立分析事务；参数：应用配置、写入开关；返回：连接和表集合。"""
    if settings.collection_backend != 'mysql' or settings.task_backend != 'mysql':
        raise ValueError('分析需要启用MySQL任务与采集存储。')
    store = collection_store(settings)
    try:
        with (store.engine.begin() if write else store.engine.connect()) as conn:
            yield conn, store.tables
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def sources(conn, tables, product, batch_id=None):
    """校验并读取整批评价；参数：连接、表、平台商品编号、可选批次；返回：商品、批次及脱敏快照。"""
    p, b, r = (tables[n] for n in ('products', 'collection_batches', 'review_observations'))
    identity = conn.execute(sa.select(p).where(p.c.platform == 'taobao', p.c.platform_product_id == product)).mappings().one_or_none()
    if not identity:
        raise LookupError('商品尚未入库。')
    condition = [b.c.product_id == identity['id'], b.c.module == 'reviews', b.c.visibility == 'published', b.c.status.in_(['complete', 'no_reviews'])]
    if batch_id:
        condition.append(b.c.id == batch_id)
    batch = conn.execute(sa.select(b).where(*condition).order_by(b.c.captured_at.desc(), b.c.id.desc()).limit(1)).mappings().one_or_none()
    if not batch:
        raise LookupError('没有属于该商品的已完成、已发布评价批次。')
    records = conn.execute(sa.select(r).where(r.c.batch_id == batch['id']).order_by(r.c.position, r.c.id).limit(1001)).mappings().all()
    if not records:
        raise ValueError('本批没有评价，无法启动分析。')
    if len(records) > 1000:
        raise ValueError('首版整批分析最多1000条；未静默截断资料。')
    snapshots = []
    for row in records:
        appends = row['append_reviews'] or []
        if not isinstance(appends, list):
            raise ValueError('追评资料结构无效，请核对来源。')
        parts = []
        for index, item in enumerate(appends, 1):
            if not isinstance(item, dict):
                raise ValueError('追评条目结构无效。')
            text = item.get('content', item.get('text', '')) or ''
            if not isinstance(text, str):
                raise ValueError('追评文字格式无效。')
            parts.append({'segment': f'append:{index}', 'text': text})
        snapshots.append({'id': row['id'], 'text': row['content'] or '', 'sku': row['sku_text'] or '', 'appends': parts})
    return dict(identity), dict(batch), snapshots


def preview(settings, product, batch_id=None):
    """读取分析候选及历史批次；参数：配置、商品和可选批次；返回：资料，不写数据库。"""
    with connection(settings) as (conn, tables):
        identity, batch, items = sources(conn, tables, product, batch_id)
        b = tables['collection_batches']
        history = [dict(r) for r in conn.execute(sa.select(b.c.id, b.c.captured_at, b.c.coverage_status, b.c.requested_limit)
            .where(b.c.product_id == identity['id'], b.c.module == 'reviews', b.c.visibility == 'published', b.c.status == 'complete')
            .order_by(b.c.captured_at.desc(), b.c.id.desc())).mappings()]
    return {'product': identity, 'batch': batch, 'items': items, 'history': history}


def create(settings, product, batch_id, rules, parameters, provider, model, rule_version, expected_hash, force=False):
    """事务创建并防止重复任务；参数：来源、固定规则和参数、预览哈希；返回：任务与是否新建。"""
    with connection(settings, True) as (conn, tables):
        p, t, src = (tables[n] for n in ('products', 'analysis_tasks', 'analysis_sources'))
        conn.execute(sa.select(p.c.id).where(p.c.platform == 'taobao', p.c.platform_product_id == product).with_for_update()).all()
        identity, batch, items = sources(conn, tables, product, batch_id)
        if fingerprint(items) != expected_hash:
            raise ValueError('资料已变化，请刷新确认后再分析。')
        digest = fingerprint({'product': identity['id'], 'batch': batch['id'], 'items': items, 'rules': rules,
                              'parameters': {k:v for k,v in parameters.items() if k not in {'max_calls','max_tokens','config_file'}},
                              'provider': provider, 'model': model, 'rule_version': rule_version})
        existing = conn.execute(sa.select(t).where(t.c.fingerprint == digest).order_by(t.c.created_at.desc(), t.c.id.desc())).mappings().all()
        active = next((r for r in existing if r['active_key']), None)
        if active or (existing and not force):
            return dict(active or existing[0]), False
        now = database_now()
        row = dict(id=uuid4().hex, product_id=identity['id'], analysis_type='reviews', fingerprint=digest, active_key=digest,
                   status='queued', provider=provider, model=model, rule_version=rule_version, rules=rules, parameters=parameters,
                   source_count=len(items), processed_count=0, usage={}, message='已保存资料，等待后台启动。', owner=None,
                   execution_task_id=None, created_at=now, updated_at=now)
        conn.execute(t.insert().values(**row))
        scope = {key: str(batch[key]) if key == 'captured_at' else batch.get(key) for key in
                 ('captured_at', 'coverage_status', 'requested_limit', 'stop_reason', 'end_confirmed', 'source_batch_key', 'task_id')}
        for position, item in enumerate(items, 1):
            conn.execute(src.insert().values(id=uuid4().hex, analysis_id=row['id'], batch_id=batch['id'], review_id=item['id'],
                position=position, snapshot=item, content_hash=fingerprint(item), scope=scope))
        return row, True


def task_row(conn, tables, identifier, lock=False):
    """读取单任务并可加行锁；参数：连接、表、编号和锁；返回：任务。"""
    t = tables['analysis_tasks']
    query = sa.select(t).where(t.c.id == identifier)
    row = conn.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    if not row:
        raise LookupError('分析任务不存在。')
    return dict(row)


def read(settings, identifier):
    """读取完整持久结果；参数：配置和任务编号；返回：任务、来源、明细、调用和最新报告。"""
    with connection(settings) as (conn, tables):
        task = task_row(conn, tables, identifier)
        value = {'task': task}
        for key, name, order in [('sources', 'analysis_sources', 'position'), ('details', 'analysis_details', 'id'),
                                 ('calls', 'analysis_calls', 'started_at'), ('reports', 'analysis_reports', 'version')]:
            table = tables[name]
            value[key] = [dict(r) for r in conn.execute(sa.select(table).where(table.c.analysis_id == identifier).order_by(table.c[order])).mappings()]
        value['report'] = value['reports'][-1] if value['reports'] else None
        p = tables['products']
        value['product'] = dict(conn.execute(sa.select(p).where(p.c.id == task['product_id'])).mappings().one())
        return value


def batch_choices(settings, product):
    """查询可定位的评价批次；参数：配置、平台编号；返回：批次元信息，不解析评价或调用AI。"""
    with connection(settings) as (conn, tables):
        p, b = tables['products'], tables['collection_batches']
        return [dict(r) for r in conn.execute(sa.select(b.c.id, b.c.captured_at, b.c.status)
            .join(p, b.c.product_id == p.c.id).where(p.c.platform == 'taobao', p.c.platform_product_id == product,
                b.c.module == 'reviews', b.c.visibility == 'published')
            .order_by(b.c.captured_at.desc(), b.c.id.desc())).mappings()]


def history(settings, product, batch_id=None):
    """查询商品或指定批次最近100次分析；参数：配置、平台编号、可选批次；返回：任务及来源、报告标记。"""
    with connection(settings) as (conn, tables):
        p, t, source, report = (tables[n] for n in ('products', 'analysis_tasks', 'analysis_sources', 'analysis_reports'))
        batches = sa.select(source.c.analysis_id, sa.func.min(source.c.batch_id).label('batch_id')).group_by(source.c.analysis_id).subquery()
        query = sa.select(t, batches.c.batch_id, sa.exists(sa.select(report.c.id).where(report.c.analysis_id == t.c.id)).label('has_report'))
        query = query.join(p, t.c.product_id == p.c.id).outerjoin(batches, batches.c.analysis_id == t.c.id).where(
            p.c.platform == 'taobao', p.c.platform_product_id == product, t.c.analysis_type == 'reviews')
        if batch_id is not None:
            query = query.where(batches.c.batch_id == batch_id)
        return [dict(r) for r in conn.execute(query.order_by(t.c.created_at.desc(), t.c.id.desc()).limit(100)).mappings()]


def latest_success(settings, product, batch_id=None):
    """在完整历史中找最近成功报告；参数：配置、商品和批次；返回：任务或空，不受列表100条限制。"""
    with connection(settings) as (conn, tables):
        t, p, s, r = (tables[n] for n in ('analysis_tasks', 'products', 'analysis_sources', 'analysis_reports'))
        batch = sa.select(sa.func.min(s.c.batch_id)).where(s.c.analysis_id == t.c.id).correlate(t).scalar_subquery()
        query = sa.select(t, batch.label('batch_id')).join(p, t.c.product_id == p.c.id).where(
            p.c.platform == 'taobao', p.c.platform_product_id == product, t.c.analysis_type == 'reviews',
            t.c.status == 'succeeded', sa.exists(sa.select(r.c.id).where(r.c.analysis_id == t.c.id)))
        if batch_id is not None:
            query = query.where(sa.exists(sa.select(s.c.id).where(s.c.analysis_id == t.c.id, s.c.batch_id == batch_id)))
        row = conn.execute(query.order_by(t.c.created_at.desc(), t.c.id.desc()).limit(1)).mappings().one_or_none()
        return dict(row) if row else None


def update_task(settings, identifier, **values):
    """安全更新任务元信息；参数：配置、编号和受控字段；返回：无。"""
    if set(values) - {'status', 'message', 'active_key', 'owner', 'execution_task_id', 'processed_count', 'usage'}:
        raise ValueError('不支持的任务更新。')
    with connection(settings, True) as (conn, tables):
        task_row(conn, tables, identifier, True)
        conn.execute(tables['analysis_tasks'].update().where(tables['analysis_tasks'].c.id == identifier)
                     .values(**values, updated_at=database_now()))


def begin_call(settings, identifier, kind, index, prompt, source_ids, reserve):
    """在联网前保存请求并扣除预算；参数：任务、请求和保守用量；返回：调用编号。"""
    with connection(settings, True) as (conn, tables):
        task = task_row(conn, tables, identifier, True)
        c = tables['analysis_calls']
        calls = conn.execute(sa.select(c.c.usage).where(c.c.analysis_id == identifier)).scalars().all()
        if len(calls) >= task['parameters']['max_calls'] or sum(max(v.get('reserved_tokens', 0), v.get('total_tokens', 0)) for v in calls) + reserve > task['parameters']['max_tokens']:
            raise ValueError('本次调用次数或保守用量预算不足；未发出新请求。')
        uid = uuid4().hex
        conn.execute(c.insert().values(id=uid, analysis_id=identifier, kind=kind, chunk_index=index, status='inflight',
            request={'prompt': prompt, 'source_ids': source_ids, 'max_output_tokens': task['parameters']['max_output_tokens']},
            response=None, usage={'reserved_tokens': reserve, 'unknown': True}, error='', started_at=database_now(), finished_at=None))
        return uid


def finish_call(settings, identifier, call_id, status, response, error='', results=None):
    """原子保存响应与整批校验明细；参数：任务、调用、状态、响应与结果；返回：无。"""
    with connection(settings, True) as (conn, tables):
        task_row(conn, tables, identifier, True)
        c, d, src, t = (tables[n] for n in ('analysis_calls', 'analysis_details', 'analysis_sources', 'analysis_tasks'))
        old = conn.execute(sa.select(c).where(c.c.id == call_id, c.c.analysis_id == identifier).with_for_update()).mappings().one()
        if old['status'] != 'inflight':
            raise ValueError('调用记录已完成，禁止重复提交。')
        usage = {'reserved_tokens': old['usage']['reserved_tokens'], 'unknown': response is None, **((response or {}).get('usage') or {})}
        conn.execute(c.update().where(c.c.id == call_id).values(status=status, response=response, usage=usage, error=error, finished_at=database_now()))
        if results is not None:
            sources_by_review = {r.review_id: r.id for r in conn.execute(sa.select(src.c.review_id, src.c.id).where(src.c.analysis_id == identifier))}
            for result in results:
                conn.execute(d.insert().values(id=uuid4().hex, analysis_id=identifier, source_id=sources_by_review[result['id']], call_id=call_id,
                    result=result, corrections=[], revision=0, review_status='unreviewed'))
        all_usage = conn.execute(sa.select(c.c.usage).where(c.c.analysis_id == identifier)).scalars().all()
        totals = {name: sum(v.get(name, 0) for v in all_usage) for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
        totals['unknown_calls'] = sum(bool(v.get('unknown')) for v in all_usage)
        count = conn.scalar(sa.select(sa.func.count()).select_from(d).where(d.c.analysis_id == identifier))
        conn.execute(t.update().where(t.c.id == identifier).values(processed_count=count, usage=totals, updated_at=database_now()))


def save_report(settings, identifier, content):
    """串行追加报告版本；参数：配置、任务和报告；返回：版本。"""
    with connection(settings, True) as (conn, tables):
        task_row(conn, tables, identifier, True)
        d = tables['analysis_details']
        revisions = {r.id: r.revision for r in conn.execute(sa.select(d.c.id, d.c.revision).where(d.c.analysis_id == identifier))}
        if revisions != content['detail_revisions']:
            raise ValueError('统计期间明细版本已变化，请重新统计；已保存的修正仍保留。')
        r = tables['analysis_reports']
        version = (conn.scalar(sa.select(sa.func.max(r.c.version)).where(r.c.analysis_id == identifier)) or 0) + 1
        conn.execute(r.insert().values(id=uuid4().hex, analysis_id=identifier, version=version, content=content, created_at=database_now()))
        return version


def correct(settings, identifier, detail_id, expected_revision, result, reason):
    """乐观锁保存人工修订；参数：任务、明细、旧版本、完整结果及原因；返回：无。"""
    with connection(settings, True) as (conn, tables):
        task = task_row(conn, tables, identifier, True)
        if task['status'] in {'queued', 'running'}:
            raise ValueError('分析运行期间不能修改结果。')
        d = tables['analysis_details']
        row = conn.execute(sa.select(d).where(d.c.id == detail_id, d.c.analysis_id == identifier).with_for_update()).mappings().one_or_none()
        if not row or row['revision'] != expected_revision:
            raise ValueError('明细版本已变化，请刷新后修改。')
        before = row['corrections'][-1]['after'] if row['corrections'] else row['result']
        changes = [*row['corrections'], {'revision': expected_revision + 1, 'reason': reason,
            'time': database_now().isoformat() + '+08:00', 'before': before, 'after': result}]
        conn.execute(d.update().where(d.c.id == detail_id).values(corrections=changes, revision=expected_revision + 1, review_status='corrected'))
