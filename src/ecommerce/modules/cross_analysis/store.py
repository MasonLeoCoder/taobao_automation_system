"""交叉分析来源固定与通用表存取，不改基础报告或采集资料。"""
import copy
from uuid import uuid4
import sqlalchemy as sa
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.modules.composite_analysis import store as text_sources
from ecommerce.modules.image_analysis import report as images
from ecommerce.modules.image_analysis.kinds import kind as image_kind
from .kinds import KINDS, TYPE, VERSION, ordered, combination

identity = text_sources.identity


def choices(conn, tables, product_id, kind):
    """按采集时间优先读取最近100份具体版本，排序不携大正文。"""
    t,r,s,b = (tables[n] for n in ('analysis_tasks','analysis_reports','analysis_sources','collection_batches'))
    batch = sa.select(sa.func.min(s.c.batch_id)).where(s.c.analysis_id == t.c.id).correlate(t).scalar_subquery()
    captured = sa.select(sa.func.max(b.c.captured_at)).join(s, s.c.batch_id == b.c.id).where(s.c.analysis_id == t.c.id).correlate(t).scalar_subquery()
    published = sa.exists(sa.select(s.c.id).join(b,b.c.id == s.c.batch_id).where(s.c.analysis_id == t.c.id,
        b.c.product_id == product_id, b.c.module == kind, b.c.visibility == 'published'))
    query = sa.select(r.c.id,r.c.analysis_id,r.c.version,r.c.created_at,t.c.model,t.c.status,t.c.source_count,
        batch.label('batch_id'),captured.label('captured_at'),
        r.c.content['validation']['counts']['pending_count'].as_integer().label('pending_count'),
        r.c.content['generation']['complete'].as_boolean().label('complete'))
    query = query.join(t,t.c.id == r.c.analysis_id).where(t.c.product_id == product_id,t.c.analysis_type == kind,
        t.c.status.not_in(['queued','running']),sa.func.length(sa.func.trim(r.c.content['markdown'].as_string())) > 0,published)
    if kind in ('main_images','detail_images'):
        query = query.where(t.c.rule_version == image_kind(kind)['version'])
    return [dict(v) for v in conn.execute(query.order_by(captured.desc(),batch.desc(),r.c.created_at.desc(),r.c.version.desc(),r.c.id.desc()).limit(100)).mappings()]


def availability(conn, tables, product_id, kind, options):
    """区分无采集、待分析、进行中及最新批无报告；返回只读说明。"""
    b,t = tables['collection_batches'],tables['analysis_tasks']
    batch = conn.execute(sa.select(b.c.id,b.c.captured_at).where(b.c.product_id == product_id,b.c.module == kind,
        b.c.visibility == 'published').order_by(b.c.captured_at.desc(),b.c.id.desc()).limit(1)).mappings().one_or_none()
    active = conn.scalar(sa.select(t.c.id).where(t.c.product_id == product_id,t.c.analysis_type == kind,
        t.c.status.in_(['queued','running'])).limit(1))
    note = '尚未采集' if batch is None else '分析中，暂无可用报告' if active and not options else '尚无可用报告，请前往分析'
    if options:
        note = '已有可用报告'
        if batch and options[0]['batch_id'] != batch['id']: note = '最新采集批次暂无可用报告，当前默认使用此前报告'
        elif active: note = '使用已保存报告，另有分析任务进行中'
    return dict(note=note, latest_batch=batch['id'] if batch else None, active=bool(active))


def load_source(conn, tables, product_id, report_id, kind):
    """校验同商品固定报告；文本只带引用原文，图片只带已有报告和图号。"""
    if kind in ('reviews','questions'):
        incoming, sources = text_sources.load_source(conn,tables,product_id,report_id,kind)
    else:
        t,r,s,b = (tables[n] for n in ('analysis_tasks','analysis_reports','analysis_sources','collection_batches'))
        row = conn.execute(sa.select(r,t.c.status,t.c.source_count,t.c.parameters,t.c.rule_version).join(t,t.c.id == r.c.analysis_id)
            .where(r.c.id == report_id,t.c.product_id == product_id,t.c.analysis_type == kind,
                   t.c.rule_version == image_kind(kind)['version'])).mappings().one_or_none()
        if not row: raise LookupError('所选图片报告不属于该商品或独立分析类别。')
        content = row['content']; original = row['parameters']['plan']['input']
        if row['status'] in ('queued','running') or not isinstance(content.get('markdown'),str) or not content['markdown'].strip():
            raise ValueError('所选图片来源尚无已结束的可读主报告。')
        if original.get('module') != kind or len(original.get('batches',[])) != 1:
            raise ValueError('图片报告不是固定单模块批次。')
        rows = conn.execute(sa.select(s).where(s.c.analysis_id == row['analysis_id']).order_by(s.c.position)).mappings().all()
        batch_id = original['batches'][0]['id']
        published = conn.scalar(sa.select(b.c.id).where(b.c.id == batch_id,b.c.product_id == product_id,
            b.c.module == kind,b.c.visibility == 'published'))
        if not rows or not published: raise ValueError('图片报告来源批次未发布或不属于本商品。')
        media = tables['media_references']
        role = 'main_image' if kind == 'main_images' else 'detail_image'
        actual_media = set(conn.scalars(sa.select(media.c.id).where(media.c.id.in_([s['media_id'] for s in rows]),
            media.c.batch_id == batch_id,media.c.role == role)))
        lookup = {}; sources = []
        for source in rows:
            snapshot = source['snapshot']
            if source['batch_id'] != batch_id or snapshot.get('module') != kind or fingerprint(snapshot) != source['content_hash']:
                raise ValueError('图片报告来源快照校验不一致。')
            if source['review_id'] is not None or source['question_id'] is not None or source['media_id'] != snapshot.get('media_id') or source['media_id'] not in actual_media:
                raise ValueError('图片来源身份不一致。')
            label = snapshot['label']; lookup[label] = source['media_id']
            for frame in snapshot.get('frames',[]): lookup[label+'｜片段'+str(frame['piece'])] = source['media_id']
            sources.append({key:copy.deepcopy(source[key]) for key in ('batch_id','review_id','question_id','media_id','snapshot','scope')})
        evidence = []
        for match in images.MARKER.finditer(content['markdown']):
            key = match[1].replace('|','｜')
            if key in lookup and not any(e['marker'] == '【'+key+'】' for e in evidence):
                evidence.append(dict(id='m:'+str(lookup[key]),marker='【'+key+'】',text='图片分析报告中的图号；本次未发送或重新查看原图片。',media_id=lookup[key],reference_type='image'))
        counts = (content.get('validation') or {}).get('counts') or {}
        incoming = dict(kind=kind,report_id=row['id'],analysis_id=row['analysis_id'],version=row['version'],
            created_at=str(row['created_at']),batch_ids=[batch_id],source_count=row['source_count'],
            scope=[dict(batch_id=batch_id,captured_at=original['batches'][0].get('captured_at'))],
            generation=copy.deepcopy(content.get('generation') or {}),pending_citations=counts.get('pending_count',0),
            unverified_basis=not counts.get('citation_count'),content_hash=fingerprint(content),markdown=content['markdown'],evidence=evidence)
        incoming['image_scope'] = dict(issues=[{key:v.get(key) for key in ('label','reason','batch_id')} for v in original.get('scope',{}).get('issues',[])],
            note=original.get('scope',{}).get('note'),allow_partial=original.get('scope',{}).get('allow_partial',False))
    incoming['label'] = KINDS[kind]
    incoming['report_marker'] = '【'+KINDS[kind]+'报告】'
    incoming['sku_scope'] = list(dict.fromkeys(e['sku'] for e in incoming['evidence'] if e.get('sku')))
    incoming['sku_scope_note'] = '仅报告引用片段明确携带的规格；空列表不代表所有SKU相同，完整范围以来源报告为准。'
    for source in sources: source.setdefault('media_id',None)
    return incoming,sources


def inputs(conn, tables, product, selection):
    """在同一事务固定2～4类来源；返回：冻结输入、去重来源行与商品。"""
    selection = ordered(selection); item = identity(conn,tables,product); reports = []; sources = {}; warnings = []
    for kind,report_id in selection.items():
        incoming, rows = load_source(conn,tables,item['id'],report_id,kind); reports.append(incoming)
        if not incoming['generation'].get('complete'): warnings.append(KINDS[kind]+'报告存在可读内容，完整性仍需留意。')
        if incoming['pending_citations'] or incoming['unverified_basis']: warnings.append(KINDS[kind]+'报告存在待核对依据，检查不证明语义或性能。')
        for row in rows:
            key = (row['batch_id'],row['review_id'],row['question_id'],row['media_id'])
            if key in sources and fingerprint(sources[key]) != fingerprint(row): raise ValueError('重复来源快照冲突。')
            sources[key] = row
    times = {s.get('captured_at') for r in reports for s in r['scope'] if s.get('captured_at')}
    if len(times) > 1: warnings.append('各来源采集时间不同，不能当作同一时点的事实；先核对规格和使用条件。')
    warnings.append('各报告可能覆盖不同SKU；引用规格仅为局部线索，不能直接认定属于同款或存在矛盾。')
    context = repository.product_context(conn,tables,item)
    context = {k:context.get(k) for k in ('platform_product_id','title','title_source_batch_id')}
    return dict(version='cross-input-v1',product=context,categories=list(selection),combination=combination(selection),
        reports=reports,warnings=warnings),list(sources.values()),item


def create(settings, product, selection, expected_hash, rules, parameters, provider, model, force=False):
    """商品行锁内重核固定输入；活动复用、成功选择、失败另建，无来源漂移。"""
    with repository.connection(settings,True) as (conn,tables):
        p,t,s = (tables[n] for n in ('products','analysis_tasks','analysis_sources'))
        conn.execute(sa.select(p.c.id).where(p.c.platform == 'taobao',p.c.platform_product_id == product).with_for_update()).all()
        incoming,sources,item = inputs(conn,tables,product,selection)
        if fingerprint(incoming) != expected_hash or fingerprint(parameters['plan']['input']) != expected_hash:
            raise ValueError('来源或商品背景已变化，请重新预览并确认。')
        if rules.get('version') != VERSION: raise ValueError('交叉分析规则版本无效。')
        digest = fingerprint(dict(product=item['id'],input=incoming,rules=rules,provider=provider,model=model,output=parameters['plan']['report_output_tokens']))
        rows = [dict(v) for v in conn.execute(sa.select(t.c.id,t.c.active_key,t.c.status).where(t.c.analysis_type == TYPE,
            t.c.fingerprint == digest,sa.or_(t.c.active_key.is_not(None),t.c.status == 'succeeded'))
            .order_by(t.c.created_at.desc(),t.c.id.desc())).mappings()]
        active = next((v for v in rows if v['active_key']),None)
        if active or rows and not force: return active or rows[0],False
        now = database_now()
        row = dict(id=uuid4().hex,product_id=item['id'],analysis_type=TYPE,fingerprint=digest,active_key=digest,status='queued',
            provider=provider,model=model,rule_version=VERSION,rules=rules,parameters=parameters,source_count=len(sources),processed_count=0,
            usage={},message=incoming['combination']+'来源版本与发送计划已固定，等待启动。',owner=None,execution_task_id=None,created_at=now,updated_at=now)
        conn.execute(t.insert().values(**row))
        for position,source in enumerate(sources,1):
            conn.execute(s.insert().values(id=uuid4().hex,analysis_id=row['id'],position=position,**source,content_hash=fingerprint(source['snapshot'])))
        return row,True


def history(settings, product):
    """只读新交叉任务轻量列，旧综合和联合图片记录不进入新列表。"""
    with repository.connection(settings) as (conn,tables):
        item = identity(conn,tables,product); t,r = tables['analysis_tasks'],tables['analysis_reports']
        query = sa.select(*(t.c[k] for k in ('id','status','created_at','model','message','owner')),
            t.c.parameters['plan']['input']['categories'].label('categories'),
            t.c.parameters['plan']['input']['combination'].as_string().label('combination')).where(
                t.c.product_id == item['id'],t.c.analysis_type == TYPE,t.c.rule_version == VERSION)
        order = (t.c.created_at.desc(),t.c.id.desc())
        rows = [dict(v) for v in conn.execute(query.order_by(*order).limit(100)).mappings()]
        latest = conn.execute(query.where(t.c.status == 'succeeded',sa.exists(sa.select(r.c.id).where(r.c.analysis_id == t.c.id)))
            .order_by(*order).limit(1)).mappings().one_or_none()
        sources = []
        if latest:
            value = conn.scalar(sa.select(t.c.parameters['plan']['input']['reports']).where(t.c.id == latest['id']))
            sources = [{k:s[k] for k in ('kind','label','report_id','version','batch_ids','created_at')} for s in value]
        return dict(tasks=rows,latest_result=dict(latest) if latest else None,latest_sources=sources)
