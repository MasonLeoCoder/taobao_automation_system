"""综合分析的固定来源与事务存取，复用通用表，不修改基础报告。"""
import copy
from uuid import uuid4
import sqlalchemy as sa
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.modules.review_analysis.v6_checks import MARKER, metadata as review_metadata
from ecommerce.modules.question_analysis.checks import metadata as question_metadata

TYPE = 'composite'
VERSION = 'composite-v1'


def identity(conn, tables, product):
    """读取固定商品身份；参数：连接、表、平台编号；返回：商品字典。"""
    p = tables['products']
    row = conn.execute(sa.select(p).where(p.c.platform == 'taobao', p.c.platform_product_id == product)).mappings().one_or_none()
    if row is None:
        raise LookupError('商品尚未入库。')
    return dict(row)


def choices(conn, tables, product_id, kind):
    """读取最近100份可读主报告的轻量候选，不将旧JSON分类稿冒充正文。"""
    t, r, s = (tables[n] for n in ('analysis_tasks', 'analysis_reports', 'analysis_sources'))
    latest = sa.select(r.c.analysis_id, sa.func.max(r.c.version).label('version')).group_by(r.c.analysis_id).subquery()
    batch = sa.select(sa.func.min(s.c.batch_id)).where(s.c.analysis_id == t.c.id).correlate(t).scalar_subquery()
    query = sa.select(r.c.id, r.c.analysis_id, r.c.version, r.c.created_at, t.c.model, t.c.status, t.c.source_count,
                      batch.label('batch_id'), r.c.content['validation']['counts']['pending_count'].as_integer().label('pending_count'))
    query = query.join(t, t.c.id == r.c.analysis_id).join(latest, sa.and_(latest.c.analysis_id == r.c.analysis_id, latest.c.version == r.c.version))
    query = query.where(t.c.product_id == product_id, t.c.analysis_type == kind, t.c.status.not_in(['queued', 'running']),
                        sa.func.length(sa.func.trim(r.c.content['markdown'].as_string())) > 0)
    return [dict(row) for row in conn.execute(query.order_by(r.c.created_at.desc(), r.c.id.desc()).limit(100)).mappings()]


def normalize_reviews(text):
    """仅给明确的评价引用加类别前缀，不改报告事实或引文。"""
    def replace(match):
        """转换明确评价标记；参数：匹配；返回：唯一类别标记。"""
        value = review_metadata(match[1])
        if not value:
            return match[0]
        label = '初评' if value['segment'] == 'initial' else '追评' + value['segment'].split(':')[1]
        return f'【评价{value["review_id"]}｜{label}】'
    return MARKER.sub(replace, text)


def load_source(conn, tables, product_id, report_id, kind):
    """严格读取同商品指定版本和相关原文；参数：报告身份；返回：冻结输入及带外键的来源。"""
    t, r, s, b = (tables[n] for n in ('analysis_tasks', 'analysis_reports', 'analysis_sources', 'collection_batches'))
    row = conn.execute(sa.select(r, t.c.status, t.c.source_count, t.c.model, t.c.rule_version, t.c.parameters)
        .join(t, r.c.analysis_id == t.c.id).where(r.c.id == report_id, t.c.product_id == product_id, t.c.analysis_type == kind)).mappings().one_or_none()
    if row is None:
        raise LookupError('所选报告不属于该商品或分析类型。')
    content = row['content']
    if row['status'] in ('queued', 'running') or not isinstance(content, dict) or not isinstance(content.get('markdown'), str) or not content['markdown'].strip():
        raise ValueError('所选来源尚无已结束的可读主报告，请先完成对应分析。')
    text = content['markdown']
    refs = set()
    for match in MARKER.finditer(text):
        meta = review_metadata(match[1]) if kind == 'reviews' else question_metadata(match[1])
        if meta:
            refs.add(meta.get('review_id', meta.get('question_id')))
    column = s.c.review_id if kind == 'reviews' else s.c.question_id
    # 元信息读取全范围，原文只加载报告实际引用的来源，不发送整批资料。
    scope_rows = conn.execute(sa.select(s.c.batch_id, s.c.scope).where(s.c.analysis_id == row['analysis_id']).order_by(s.c.position)).mappings().all()
    if not scope_rows:
        raise ValueError('基础报告缺少固定来源，未发送。')
    batches = sorted({v['batch_id'] for v in scope_rows})
    published = conn.execute(sa.select(b.c.id).where(b.c.id.in_(batches), b.c.product_id == product_id,
        b.c.module == kind, b.c.visibility == 'published')).scalars().all()
    if set(published) != set(batches):
        raise ValueError('基础报告来源批次归属或发布状态不一致，未发送。')
    rows = conn.execute(sa.select(s).where(s.c.analysis_id == row['analysis_id'], column.in_(refs)).order_by(s.c.position)).mappings().all() if refs else []
    evidence = []; kept = []
    for source in rows:
        snapshot = source['snapshot']; entries = []
        if fingerprint(snapshot) != source['content_hash']:
            raise ValueError('基础报告原文快照校验不一致，未发送。')
        if kind == 'reviews':
            entries = [('initial', snapshot.get('text', ''))] + [(a['segment'], a['text']) for a in snapshot.get('appends', [])]
            for segment, original in entries:
                label = '初评' if segment == 'initial' else '追评' + segment.split(':')[1]
                marker = f'【评价{source["review_id"]}｜{label}】'
                if any(review_metadata(m[1]) == {'review_id': source['review_id'], 'segment': segment} for m in MARKER.finditer(text)):
                    evidence.append({'id': f'r:{source["review_id"]}:{segment}', 'marker': marker, 'text': original, 'sku': snapshot.get('sku', '')})
        else:
            entries = [(None, snapshot.get('text', ''))] + [(a['id'], a['text']) for a in snapshot.get('answers', [])]
            for answer_id, original in entries:
                meta = {'question_id': source['question_id'], 'answer_id': answer_id}
                if any(question_metadata(m[1]) == meta for m in MARKER.finditer(text)):
                    marker = f'【问题{source["question_id"]}' + (f'｜回答{answer_id}' if answer_id else '') + '】'
                    answer = next((a for a in snapshot.get('answers', []) if a['id'] == answer_id), {})
                    evidence.append({'id': f'q:{source["question_id"]}:{answer_id or "question"}', 'marker': marker,
                                     'text': original, 'question': snapshot.get('text', ''), 'sku': answer.get('sku', '')})
        # 原完整快照仅留本地以供回查，发送只用上方命中的具体段。
        kept.append({k: copy.deepcopy(source[k]) for k in ('batch_id', 'review_id', 'question_id', 'snapshot', 'scope')})
    counts = (content.get('validation') or {}).get('counts') or {}
    incoming = {'kind': kind, 'report_id': row['id'], 'analysis_id': row['analysis_id'], 'version': row['version'],
        'created_at': str(row['created_at']), 'batch_ids': batches, 'source_count': row['source_count'],
        'scope': list({v['batch_id']: dict(batch_id=v['batch_id'], **(v['scope'] or {})) for v in scope_rows}.values()),
        'generation': copy.deepcopy(content.get('generation') or {}), 'pending_citations': counts.get('pending_count', 0),
        'answer_count': (content.get('basic_statistics') or {}).get('answer_count'),
        'unverified_basis': not bool(counts.get('citation_count')), 'content_hash': fingerprint(content),
        'markdown': normalize_reviews(text) if kind == 'reviews' else text, 'evidence': evidence}
    # 不向AI发送机器路径、任务日志或抓取入口等内部信息。
    incoming['scope'] = [{k: v.get(k) for k in ('batch_id', 'captured_at', 'coverage_status', 'requested_limit', 'stop_reason')} for v in incoming['scope']]
    return incoming, kept


def inputs(conn, tables, product, review_report, question_report):
    """在同一事务中冻结两份来源；参数：商品与两个报告编号；返回：输入、来源与商品。"""
    item = identity(conn, tables, product)
    reviews, first = load_source(conn, tables, item['id'], review_report, 'reviews')
    questions, second = load_source(conn, tables, item['id'], question_report, 'questions')
    context = repository.product_context(conn, tables, item)
    value = {'version': 'composite-input-v1', 'product': context, 'reports': [reviews, questions]}
    return value, first + second, item


def create(settings, product, report_ids, expected_hash, rules, parameters, provider, model, force=False):
    """商品行锁内重核快照并创建；活动复用、成功明确选择、失败默认另建。"""
    with repository.connection(settings, True) as (conn, tables):
        p, t, s = (tables[n] for n in ('products', 'analysis_tasks', 'analysis_sources'))
        conn.execute(sa.select(p.c.id).where(p.c.platform == 'taobao', p.c.platform_product_id == product).with_for_update()).all()
        incoming, sources, item = inputs(conn, tables, product, *report_ids)
        if fingerprint(incoming) != expected_hash or fingerprint(parameters['plan']['input']) != expected_hash:
            raise ValueError('来源或商品背景已变化，请重新预览并确认。')
        digest = fingerprint({'product': item['id'], 'input': incoming, 'rules': rules, 'provider': provider,
                              'model': model, 'output': parameters['plan']['report_output_tokens']})
        existing = conn.execute(sa.select(t).where(t.c.analysis_type == TYPE, t.c.fingerprint == digest,
            sa.or_(t.c.active_key.is_not(None), t.c.status == 'succeeded')).order_by(t.c.created_at.desc(), t.c.id.desc())).mappings().all()
        active = next((v for v in existing if v['active_key']), None)
        if active or existing and not force:
            return dict(active or existing[0]), False
        now = database_now()
        row = dict(id=uuid4().hex, product_id=item['id'], analysis_type=TYPE, fingerprint=digest, active_key=digest,
            status='queued', provider=provider, model=model, rule_version=VERSION, rules=rules, parameters=parameters,
            source_count=len(sources), processed_count=0, usage={}, message='两份报告与发送内容已固定，等待后台启动。',
            owner=None, execution_task_id=None, created_at=now, updated_at=now)
        conn.execute(t.insert().values(**row))
        for pos, source in enumerate(sources, 1):
            conn.execute(s.insert().values(id=uuid4().hex, analysis_id=row['id'], position=pos,
                **source, content_hash=fingerprint(source['snapshot'])))
        return row, True


def history(settings, product):
    """读取独立综合历史轻量列，避免大正文参与排序；参数：商品；返回：历史与成功任务。"""
    with repository.connection(settings) as (conn, tables):
        item = identity(conn, tables, product); t, r = tables['analysis_tasks'], tables['analysis_reports']
        fields = [t.c[k] for k in ('id', 'status', 'created_at', 'model', 'message', 'owner')]
        for index, prefix in ((0, 'review'), (1, 'question')):
            fields.extend([t.c.parameters['plan']['input']['reports'][index]['batch_ids'][0].as_integer().label(prefix+'_batch'),
                           t.c.parameters['plan']['input']['reports'][index]['version'].as_integer().label(prefix+'_version')])
        query = sa.select(*fields).where(t.c.product_id == item['id'], t.c.analysis_type == TYPE)
        order = (t.c.created_at.desc(), t.c.id.desc())
        rows = [dict(v) for v in conn.execute(query.order_by(*order).limit(100)).mappings()]
        success = conn.execute(query.where(t.c.status == 'succeeded', sa.exists(sa.select(r.c.id).where(r.c.analysis_id == t.c.id)))
            .order_by(*order).limit(1)).mappings().one_or_none()
        sources = []
        if success:
            for index in (0, 1):
                fields = [t.c.parameters['plan']['input']['reports'][index][key].label(key) for key in
                          ('kind','report_id','version','batch_ids')]
                source = conn.execute(sa.select(*fields).where(t.c.id == success['id'])).mappings().one()
                sources.append(dict(source))
        return {'tasks': rows, 'latest_result': dict(success) if success else None, 'latest_sources': sources}
