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


def sources(conn, tables, product, batch_id=None, *, full_batch=False):
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
    query=sa.select(r).where(r.c.batch_id == batch['id']).order_by(r.c.position,r.c.id)
    records=conn.execute(query if full_batch else query.limit(1001)).mappings().all()
    if not records:
        raise ValueError('本批没有评价，无法启动分析。')
    if not full_batch and len(records) > 1000:
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


def preview(settings, product, batch_id=None, *, full_batch=False):
    """读取分析候选及历史批次；参数：配置、商品和可选批次；返回：资料，不写数据库。"""
    with connection(settings) as (conn, tables):
        identity, batch, items = sources(conn, tables, product, batch_id,full_batch=full_batch)
        b = tables['collection_batches']
        history = [dict(r) for r in conn.execute(sa.select(b.c.id, b.c.captured_at, b.c.coverage_status, b.c.requested_limit)
            .where(b.c.product_id == identity['id'], b.c.module == 'reviews', b.c.visibility == 'published', b.c.status == 'complete')
            .order_by(b.c.captured_at.desc(), b.c.id.desc())).mappings()]
        context = product_context(conn, tables, identity)
    return {'product': identity, 'batch': batch, 'items': items, 'history': history, 'context': context}


def product_context(conn, tables, identity):
    """读取有来源的商品背景，不从标题猜类目；参数：连接、表和商品；返回：允许发送的背景。"""
    context = {'platform_product_id': identity['platform_product_id'], 'title': identity.get('display_title') or None,
               'title_source_batch_id': identity.get('title_source_batch_id'), 'category': None, 'category_source_batch_id': None}
    b, info = tables['collection_batches'], tables.get('product_info_observations')
    if info is not None:
        rows = conn.execute(sa.select(info).join(b, info.c.batch_id == b.c.id).where(
            info.c.product_id == identity['id'], b.c.visibility == 'published', b.c.status == 'complete')
            .order_by(info.c.captured_at.desc(), info.c.id.desc())).mappings()
        for row in rows:
            if row['title'] and (row['field_states'] or {}).get('title') == 'provided':
                context.update(title=row['title'], title_source_batch_id=row['batch_id'])
                break
    p = tables.get('parameter_observations')
    if p is not None:
        rows = conn.execute(sa.select(p).join(b, p.c.batch_id == b.c.id).where(
            b.c.product_id == identity['id'], b.c.visibility == 'published', b.c.status == 'complete',
            p.c.name.in_(['类目', '商品类目'])).order_by(b.c.captured_at.desc(), b.c.id.desc())).mappings().all()
        if rows:
            latest = rows[0]['batch_id']
            values = {row['value'] for row in rows if row['batch_id'] == latest and row['value']}
            if len(values) == 1:
                context.update(category=values.pop(), category_source_batch_id=latest)
    return context


def create(settings, product, batch_id, rules, parameters, provider, model, rule_version, expected_hash, force=False, *, analysis_type="reviews"):
    """事务创建并防止重复任务；参数：来源、固定规则和参数、预览哈希；返回：任务与是否新建。"""
    with connection(settings, True) as (conn, tables):
        p, t, src = (tables[n] for n in ('products', 'analysis_tasks', 'analysis_sources'))
        conn.execute(sa.select(p.c.id).where(p.c.platform == 'taobao', p.c.platform_product_id == product).with_for_update()).all()
        if analysis_type == 'questions':
            from ecommerce.modules.question_analysis.store import sources as question_sources
            identity, batch, items = question_sources(conn,tables,product,batch_id)
        elif analysis_type == 'reviews':
            identity, batch, items = sources(conn, tables, product, batch_id,full_batch=rule_version=='reviews-v6')
        else:raise ValueError('分析类型无效。')
        if fingerprint(items) != expected_hash:
            raise ValueError('资料已变化，请刷新确认后再分析。')
        if parameters.get('plan') and product_context(conn, tables, identity) != parameters['plan']['context']:
            raise ValueError('商品背景已变化，请刷新发送预览后再分析。')
        digest = fingerprint({'product': identity['id'], 'batch': batch['id'], 'items': items, 'rules': rules,
                              'parameters': {k:v for k,v in parameters.items() if k not in {'max_calls','max_tokens','config_file','budget_history','budget_mode','validation_history','supplement_history','active_supplement'}},
                              'provider': provider, 'model': model, 'rule_version': rule_version})
        if rule_version in ('reviews-v6','questions-v1'):
            # 成品身份只含影响分析结果的内容，不因价格、预算、分词器或容量资料更新而重收费用。
            digest=fingerprint({'product':identity['id'],'batch':batch['id'],'items':items,'rules':rules,
                'title':parameters['plan']['context'].get('title'),'provider':provider,'model':model,'rule_version':rule_version,
                'report_output_tokens':parameters['plan']['report_output_tokens']})
        existing = conn.execute(sa.select(t).where(t.c.fingerprint == digest).order_by(t.c.created_at.desc(), t.c.id.desc())).mappings().all()
        active = next((r for r in existing if r['active_key']), None)
        if active or (existing and not force):
            selected=next((r for r in existing if r['status']=='succeeded'),existing[0]) if rule_version=='reviews-v6' else existing[0]
            return dict(active or selected), False
        now = database_now()
        row = dict(id=uuid4().hex, product_id=identity['id'], analysis_type=analysis_type, fingerprint=digest, active_key=digest,
                   status='queued', provider=provider, model=model, rule_version=rule_version, rules=rules, parameters=parameters,
                   source_count=len(items), processed_count=0, usage={}, message='已保存资料，等待后台启动。', owner=None,
                   execution_task_id=None, created_at=now, updated_at=now)
        conn.execute(t.insert().values(**row))
        scope = {key: str(batch[key]) if key == 'captured_at' else batch.get(key) for key in
                 ('captured_at', 'coverage_status', 'requested_limit', 'stop_reason', 'end_confirmed', 'source_batch_key', 'task_id')}
        for position, item in enumerate(items, 1):
            conn.execute(src.insert().values(id=uuid4().hex, analysis_id=row['id'], batch_id=batch['id'], review_id=item['id'] if analysis_type=='reviews' else None,
                **({'question_id':item['id']} if analysis_type=='questions' else {}),
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


def batch_choices(settings, product, *, analysis_type='reviews'):
    """查询可定位的评价批次；参数：配置、平台编号；返回：批次元信息，不解析评价或调用AI。"""
    with connection(settings) as (conn, tables):
        p, b = tables['products'], tables['collection_batches']
        return [dict(r) for r in conn.execute(sa.select(b.c.id, b.c.captured_at, b.c.status)
            .join(p, b.c.product_id == p.c.id).where(p.c.platform == 'taobao', p.c.platform_product_id == product,
                b.c.module == analysis_type, b.c.visibility == 'published')
            .order_by(b.c.captured_at.desc(), b.c.id.desc())).mappings()]


def history(settings, product, batch_id=None, *, analysis_type="reviews", metadata_only=False):
    """查询商品或指定批次最近100次分析；参数：配置、平台编号、可选批次；返回：任务及来源、报告标记。"""
    with connection(settings) as (conn, tables):
        p, t, source, report = (tables[n] for n in ('products', 'analysis_tasks', 'analysis_sources', 'analysis_reports'))
        batches = sa.select(source.c.analysis_id, sa.func.min(source.c.batch_id).label('batch_id')).group_by(source.c.analysis_id).subquery()
        fields=[t.c[k] for k in ('id','status','active_key','created_at','owner')] if metadata_only else [t]
        query = sa.select(*fields, batches.c.batch_id, sa.exists(sa.select(report.c.id).where(report.c.analysis_id == t.c.id)).label('has_report'))
        query = query.join(p, t.c.product_id == p.c.id).outerjoin(batches, batches.c.analysis_id == t.c.id).where(
            p.c.platform == 'taobao', p.c.platform_product_id == product, t.c.analysis_type == analysis_type)
        if batch_id is not None:
            query = query.where(batches.c.batch_id == batch_id)
        return [dict(r) for r in conn.execute(query.order_by(t.c.created_at.desc(), t.c.id.desc()).limit(100)).mappings()]


def latest_success(settings, product, batch_id=None, *, analysis_type='reviews', metadata_only=False):
    """在完整历史中找最近成功报告；参数：配置、商品和批次；返回：任务或空，不受列表100条限制。"""
    with connection(settings) as (conn, tables):
        t, p, s, r = (tables[n] for n in ('analysis_tasks', 'products', 'analysis_sources', 'analysis_reports'))
        batch = sa.select(sa.func.min(s.c.batch_id)).where(s.c.analysis_id == t.c.id).correlate(t).scalar_subquery()
        fields=[t.c[k] for k in ('id','status','active_key','created_at','owner')] if metadata_only else [t]
        query = sa.select(*fields, batch.label('batch_id')).join(p, t.c.product_id == p.c.id).where(
            p.c.platform == 'taobao', p.c.platform_product_id == product, t.c.analysis_type == analysis_type,
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


def charged_usage(usage, version=1):
    """核算已占额度；参数：用量及预算版本；返回：确认实际量或未知预留量。"""
    if version >= 2 and usage.get('unknown') is False and type(usage.get('total_tokens')) is int:
        return usage['total_tokens']
    return max(usage.get('reserved_tokens', 0), usage.get('total_tokens', 0))


def budget_status(parameters, calls):
    """汇总任务额度；参数：固定参数与调用；返回：用量、剩余额度及调用次数。"""
    used = sum(charged_usage(call['usage'], parameters.get('budget_version', 1)) for call in calls)
    return {'used_tokens': used, 'remaining_tokens': parameters['max_tokens'] - used,
            'used_calls': len(calls), 'remaining_calls': parameters['max_calls'] - len(calls)}


def begin_call(settings, identifier, kind, index, prompt, source_ids, reserve, *, output_tokens=None, json_mode=False, timeout_seconds=None, report_input=None,messages=None,measurement=None):
    """在联网前保存请求并扣除预算；参数：任务、请求和保守用量；返回：调用编号。"""
    with connection(settings, True) as (conn, tables):
        task = task_row(conn, tables, identifier, True)
        c = tables['analysis_calls']
        calls = conn.execute(sa.select(c).where(c.c.analysis_id == identifier)).mappings().all()
        budget = budget_status(task['parameters'], calls)
        held = task['parameters'].get('plan', {}).get('report_reserved_tokens', 0) if kind == 'classify' else 0
        if budget['remaining_calls'] < 1 or budget['remaining_tokens'] < reserve + held:
            raise ValueError('本次调用次数或AI用量上限不足；未发出新请求，已保存结果保留。可明确增加额度后继续。')
        uid = uuid4().hex
        request = {'prompt': prompt, 'source_ids': source_ids, 'max_output_tokens': output_tokens or task['parameters']['max_output_tokens']}
        if messages is not None:request.update(messages=messages,measurement=measurement,request_hash=fingerprint(messages))
        if report_input is not None:
            request['report_input'] = report_input
        if task['parameters'].get('plan'):
            request.update(json_mode=json_mode, timeout_seconds=timeout_seconds)
        if task['parameters'].get('active_supplement'):
            request['supplement_id'] = task['parameters']['active_supplement']['id']
        conn.execute(c.insert().values(id=uid, analysis_id=identifier, kind=kind, chunk_index=index, status='inflight',
            request=request,
            response=None, usage={'reserved_tokens': reserve, 'unknown': True}, error='', started_at=database_now(), finished_at=None))
        return uid


def save_results(conn, tables, identifier, call_id, results, allowed_ids):
    """仅新增同调用来源中尚未保存的合格明细；参数：事务、来源及结果；返回：新增评价编号。"""
    d, src = tables['analysis_details'], tables['analysis_sources']
    source_rows = conn.execute(sa.select(src.c.review_id, src.c.id).where(src.c.analysis_id == identifier)).all()
    source_ids = {row.review_id:row.id for row in source_rows}
    existing = set(conn.execute(sa.select(d.c.source_id).where(d.c.analysis_id == identifier)).scalars())
    ids = [result['id'] for result in results]
    if len(ids) != len(set(ids)) or not set(ids) <= set(allowed_ids) or not set(ids) <= set(source_ids):
        raise ValueError('校验结果编号与本次调用来源不一致，未保存。')
    added = []
    for result in results:
        if source_ids[result['id']] in existing:
            continue
        conn.execute(d.insert().values(id=uuid4().hex, analysis_id=identifier, source_id=source_ids[result['id']], call_id=call_id,
            result=result, corrections=[], revision=0, review_status='unreviewed'))
        added.append(result['id'])
    return added


def persist_validation(conn, tables, task, call, assessment, origin):
    """保存不可覆盖的校验审计及合格条目；参数：事务、快照和评估；返回：新增及审计标记。"""
    if set(assessment['source_ids']) != set(call['request']['source_ids']):
        raise ValueError('本地校验范围与原调用不一致。')
    src = tables['analysis_sources']
    hashes = [(row.review_id,row.content_hash) for row in conn.execute(sa.select(src.c.review_id,src.c.content_hash)
        .where(src.c.analysis_id == task['id'],src.c.review_id.in_(assessment['source_ids'])).order_by(src.c.position))]
    response_hash = fingerprint(call['response'])
    digest = fingerprint({'call_id':call['id'],'policy':assessment['policy'],'response_hash':response_hash,'sources':hashes})
    parameters = dict(task['parameters'])
    history = parameters.get('validation_history', [])
    if any(record['digest'] == digest for record in history):
        return {'added_ids':[],'recorded':False}
    added = save_results(conn,tables,task['id'],call['id'],assessment['results'],assessment['source_ids'])
    record = {'id':uuid4().hex,'version':1,'policy':assessment['policy'],'digest':digest,'call_id':call['id'],
        'response_hash':response_hash,'source_hash':fingerprint(hashes),'origin':origin,
        'time':database_now().isoformat()+'+08:00','source_ids':assessment['source_ids'],
        'accepted_ids':[result['id'] for result in assessment['results']],'added_ids':added,
        'issues':assessment['issues'],'adjustments':[change for change in assessment['adjustments'] if change['review_id'] in added]}
    if 'outcomes' in assessment:
        record.update(outcomes=assessment['outcomes'],technical_failure=assessment['technical_failure'])
    parameters['validation_history'] = [*history,record]
    count = conn.scalar(sa.select(sa.func.count()).select_from(tables['analysis_details']).where(tables['analysis_details'].c.analysis_id == task['id']))
    conn.execute(tables['analysis_tasks'].update().where(tables['analysis_tasks'].c.id == task['id']).values(
        parameters=parameters,processed_count=count,updated_at=database_now()))
    task['parameters'] = parameters
    return {'added_ids':added,'recorded':True}


def recover_call(settings, identifier, call_id, assessment, expected_response_hash):
    """从终态原响应恢复有效条目，原调用及原始返回保持不变；参数：评估和响应指纹；返回：新增情况。"""
    with connection(settings, True) as (conn,tables):
        task = task_row(conn,tables,identifier,True)
        c = tables['analysis_calls']
        call = conn.execute(sa.select(c).where(c.c.id == call_id,c.c.analysis_id == identifier).with_for_update()).mappings().one()
        if call['kind'] != 'classify' or call['status'] == 'inflight' or call['response'] is None:
            raise ValueError('该调用没有可本地恢复的终态分类返回。')
        if fingerprint(call['response']) != expected_response_hash:
            raise ValueError('原始返回已变化，请刷新后重新校验。')
        return persist_validation(conn,tables,task,call,assessment,'local')


def finish_call(settings, identifier, call_id, status, response, error='', results=None, assessment=None):
    """原子保存原始响应、逐条校验及问题审计；参数：任务、调用和评估；返回：无。"""
    with connection(settings, True) as (conn, tables):
        task = task_row(conn, tables, identifier, True)
        c, d, src, t = (tables[n] for n in ('analysis_calls', 'analysis_details', 'analysis_sources', 'analysis_tasks'))
        old = conn.execute(sa.select(c).where(c.c.id == call_id, c.c.analysis_id == identifier).with_for_update()).mappings().one()
        if old['status'] != 'inflight':
            raise ValueError('调用记录已完成，禁止重复提交。')
        provided = (response or {}).get('usage') or {}
        usage = {'reserved_tokens': old['usage']['reserved_tokens'], **provided,
                 'unknown': response is None or type(provided.get('total_tokens')) is not int}
        conn.execute(c.update().where(c.c.id == call_id).values(status=status, response=response, usage=usage, error=error, finished_at=database_now()))
        if assessment is not None:
            persist_validation(conn,tables,task,{**dict(old),'response':response},assessment,'online')
        elif results is not None:
            save_results(conn,tables,identifier,call_id,results,old['request']['source_ids'])
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


def check_inputs(data):
    """绑定检查时的来源和原调用快照；参数：同次聚合；返回：规范哈希，不包含本机密钥。"""
    return fingerprint({'sources':[(s['id'],s['content_hash'],s['snapshot']) for s in sorted(data['sources'],key=lambda s:s['position'])],
        'calls':[(c['id'],c['status'],c['request'],c['response']) for c in sorted(data['calls'],key=lambda c:c['id'])]})


def save_local_report(settings,identifier,content,expected_report_id,expected_inputs,updates):
    """终态任务行锁内追加本地检查及状态；参数：报告、原版本/输入哈希、状态；返回：版本，冲突不写入。"""
    if set(updates)!={'status','message','owner','active_key'} or updates['owner'] is not None or updates['active_key'] is not None:
        raise ValueError('本地检查状态更新无效。')
    with connection(settings,True) as (conn,tables):
        task=task_row(conn,tables,identifier,True)
        if task['status'] in {'queued','running'}:raise ValueError('分析运行期间不能重新检查报告。')
        r=tables['analysis_reports'];latest=conn.execute(sa.select(r).where(r.c.analysis_id==identifier).order_by(r.c.version.desc()).limit(1)).mappings().one_or_none()
        if (latest['id'] if latest else None)!=expected_report_id:raise ValueError('报告版本已变化，请刷新后重新检查。')
        current={}
        for key,name in (('sources','analysis_sources'),('calls','analysis_calls')):
            current[key]=[dict(row) for row in conn.execute(sa.select(tables[name]).where(tables[name].c.analysis_id==identifier)).mappings()]
        if check_inputs(current)!=expected_inputs:raise ValueError('检查期间来源或调用已变化，未保存过期检查结果。')
        version=(latest['version'] if latest else 0)+1
        conn.execute(r.insert().values(id=uuid4().hex,analysis_id=identifier,version=version,content=content,created_at=database_now()))
        conn.execute(tables['analysis_tasks'].update().where(tables['analysis_tasks'].c.id==identifier).values(**updates,updated_at=database_now()))
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
