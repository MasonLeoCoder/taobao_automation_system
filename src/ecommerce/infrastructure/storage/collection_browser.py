"""资料浏览的只读查询；复用正式批次、观察和有效分区，不改变采集数据。"""
import re
from datetime import date, datetime, time, timedelta

import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_store import collection_store
from ecommerce.infrastructure.storage.collection_queries import public, _page
from ecommerce.infrastructure.storage.batch_counts import attach_batch_counts
from ecommerce.infrastructure.storage.mysql import MySQLStorageError, safe_error


def browse(settings, view, **options):
    """执行资料查询；参数：配置、视图和筛选；返回：安全序列化结果。"""
    store = collection_store(settings)
    try:
        with store.engine.connect() as connection:
            return public(_browse(connection, store.tables, view, **options))
    except sa.exc.SQLAlchemyError as error:
        raise MySQLStorageError(safe_error(error)) from None


def _text(value):
    """校验查询文字；参数：文本；返回：去空白文本。"""
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError('筛选文字最多200字')
    return value.strip()


def _browse(c, tables, view, **o):
    """构造只读资料视图；参数：连接、表集合、视图及筛选；返回：字典。"""
    p, b, l, m, f, current, tasks = [tables[n] for n in (
        'products', 'collection_batches', 'list_observations', 'media_references',
        'file_assets', 'product_current_sections', 'tasks')]
    visible = b.c.visibility == 'published'
    dates=[]
    date_bounds={}
    for key in ('since','until'):
        value=o.get(key,'')
        if value:
            try:
                parsed=datetime.combine(date.fromisoformat(value),time.min)
                if key=='until':
                    parsed+=timedelta(days=1)
            except (ValueError,OverflowError):
                raise ValueError('日期须为有效的年-月-日') from None
            dates.append(b.c.captured_at>=parsed if key=='since' else b.c.captured_at<parsed)
            date_bounds[key]=parsed
    if o.get('since') and o.get('until') and o['since']>o['until']:
        raise ValueError('开始日期不能晚于结束日期')
    if view=='catalog':
        from ecommerce.infrastructure.storage.product_catalog import product_catalog
        return product_catalog(c,tables,o,date_bounds)
    if view=='keyword_products':
        from ecommerce.infrastructure.storage.keyword_products import keyword_products
        return keyword_products(c,tables,o,date_bounds)
    if view == 'file':
        asset = c.execute(sa.select(f).where(f.c.id == o['file_id'], f.c.availability == 'present',
            sa.exists(sa.select(m.c.id).join(b, b.c.id == m.c.batch_id).where(
                m.c.file_asset_id == f.c.id, visible,
                m.c.role.in_(['main_image','detail_image','main_video','sku_image','list_image',
                    'review_image','review_video','review_poster','append_image','append_video','append_poster',
                    'answer_image','answer_video','answer_poster']))))).mappings().first()
        if not asset:
            raise LookupError('文件不存在或没有已发布的素材来源')
        return dict(asset)
    if view in {'products','details'}:
        after, limit = o.get('after', 0), o.get('limit', 30)
        _page(after, limit)
        keyword, source = _text(o.get('keyword', '')), _text(o.get('source', ''))
        observed = sa.exists(sa.select(l.c.id).join(b, b.c.id == l.c.batch_id).where(l.c.product_id == p.c.id, visible))
        detailed = sa.exists(sa.select(b.c.id).where(b.c.product_id == p.c.id, visible))
        entries=tables['search_records']
        opened=sa.exists(sa.select(entries.c.id).where(entries.c.product_id==p.c.id))
        statement = sa.select(p).where(p.c.id > after, sa.or_(observed, detailed,opened))
        if view=='details':
            runs=tables['collection_runs']
            opened=sa.exists(sa.select(entries.c.id).where(entries.c.product_id==p.c.id,entries.c.record_type=='detail_open'))
            attempted=sa.exists(sa.select(runs.c.id).where(runs.c.product_id==p.c.id))
            statement=sa.select(p).where(p.c.id>after,p.c.platform=='taobao',sa.or_(detailed,opened,attempted))
        if keyword:
            statement = statement.where(sa.or_(p.c.platform_product_id == keyword, p.c.display_title.contains(keyword, autoescape=True)))
        if source:
            statement = statement.where(sa.exists(sa.select(l.c.id).join(b, b.c.id == l.c.batch_id).where(
                l.c.product_id == p.c.id, visible, b.c.search_keyword == source)))
        if dates:
            if view=='details':
                matching=sa.exists(sa.select(b.c.id).where(visible,b.c.product_id==p.c.id,*dates))
                for table,time_column in [(runs,runs.c.started_at),(entries,entries.c.captured_at)]:
                    conditions=[table.c.product_id==p.c.id]
                    if table is entries:
                        conditions.append(entries.c.record_type=='detail_open')
                    if 'since' in date_bounds: conditions.append(time_column>=date_bounds['since'])
                    if 'until' in date_bounds: conditions.append(time_column<date_bounds['until'])
                    matching=sa.or_(matching,sa.exists(sa.select(table.c.id).where(*conditions)))
                statement=statement.where(matching)
            else:
                statement=statement.where(sa.exists(sa.select(b.c.id).where(visible,*dates,
                    sa.or_(b.c.product_id==p.c.id,sa.exists(sa.select(l.c.id).where(l.c.batch_id==b.c.id,l.c.product_id==p.c.id))))))
        state=o.get('state','')
        valid=sa.exists(sa.select(current.c.batch_id).join(b,b.c.id==current.c.batch_id).where(current.c.product_id==p.c.id,visible))
        if state=='effective':
            statement=statement.where(valid)
        elif state=='attempt':
            statement=statement.where(detailed,~valid)
        elif state=='list':
            statement=statement.where(~detailed)
        elif state:
            raise ValueError('资料情况筛选无效')
        if o.get('page') is not None:
            from ecommerce.infrastructure.storage.pagination import numbered_rows
            from ecommerce.infrastructure.storage.sorting import sorted_statement
            listed=sa.exists(sa.select(l.c.id).where(l.c.batch_id==b.c.id,l.c.product_id==p.c.id).correlate(b,p))
            own=sa.or_(b.c.product_id==p.c.id,listed)
            captured=sa.select(sa.func.max(b.c.captured_at)).where(visible,own).correlate(p).scalar_subquery()
            if view=='details':
                captured=sa.func.coalesce(sa.select(sa.func.max(runs.c.started_at)).where(runs.c.product_id==p.c.id).correlate(p).scalar_subquery(),
                    sa.select(sa.func.max(b.c.captured_at)).where(visible,b.c.product_id==p.c.id).correlate(p).scalar_subquery(),
                    sa.select(sa.func.max(entries.c.captured_at)).where(entries.c.product_id==p.c.id,entries.c.record_type=='detail_open').correlate(p).scalar_subquery())
            price=sa.select(l.c.price_value).join(b,b.c.id==l.c.batch_id).where(l.c.product_id==p.c.id,visible).order_by(
                b.c.captured_at.desc(),l.c.id.desc()).limit(1).correlate(p).scalar_subquery()
            module_count=sa.select(sa.func.count(sa.distinct(b.c.module))).where(visible,b.c.product_id==p.c.id).correlate(p).scalar_subquery()
            columns={'captured_at':captured,'title':p.c.display_title,'price':price,'modules':module_count}
            ordered,spec=sorted_statement(statement,columns,p.c.id,o.get('sort_by'),o.get('order'),'captured_at','desc')
            rows,page_info=numbered_rows(c,ordered,o['page'])
            page_info['sort_spec']=spec
            more=page_info['has_more']
        else:
            rows = [dict(r) for r in c.execute(statement.order_by(p.c.id).limit(limit + 1)).mappings()]
            more, rows = len(rows) > limit, rows[:limit]
        for row in rows:
            own = sa.or_(b.c.product_id == row['id'], sa.exists(sa.select(l.c.id).where(l.c.batch_id == b.c.id, l.c.product_id == row['id'])))
            row['captured_at'] = c.scalar(sa.select(sa.func.max(b.c.captured_at)).where(visible, own))
            row['modules'] = list(c.execute(sa.select(b.c.module).where(visible, b.c.product_id == row['id']).distinct()).scalars())
            latest = c.execute(sa.select(l.c.price_text, l.c.sales_text, l.c.shop_name).join(b, b.c.id == l.c.batch_id).where(
                l.c.product_id == row['id'], visible).order_by(b.c.captured_at.desc(), l.c.id.desc()).limit(1)).mappings().first()
            row['listing'] = dict(latest or {})
            from ecommerce.infrastructure.storage.collection_context import detail_summary
            row['detail_summary']=detail_summary(c,tables,row['id'])
            if view=='details':
                row['captured_at']=row['detail_summary']['last_attempt_at']
            row['thumbnail'] = c.scalar(sa.select(m.c.file_asset_id).join(b, b.c.id == m.c.batch_id).join(f, f.c.id == m.c.file_asset_id).where(
                visible, b.c.product_id == row['id'], m.c.role == 'main_image', f.c.availability == 'present')
                .order_by(b.c.captured_at.desc(), m.c.position).limit(1))
        return {'products': rows, 'has_more': more, 'next_cursor': rows[-1]['id'] if rows else after,
            **(page_info if o.get('page') is not None else {})}
    if view=='keyword_groups':
        source=_text(o.get('source',''))
        statement=sa.select(b.c.id,b.c.task_id,b.c.search_keyword,b.c.captured_at).where(visible,b.c.module=='list',*dates)
        if source: statement=statement.where(b.c.search_keyword==source)
        filtered=statement.subquery('keyword_batches')
        category=sa.func.nullif(filtered.c.search_keyword,'')
        groups=sa.select(sa.func.min(filtered.c.id).label('id'),category.label('keyword'),
            sa.func.max(filtered.c.captured_at).label('captured_at'),sa.func.count().label('pages'),
            (sa.func.count(sa.distinct(filtered.c.task_id))+sa.func.sum(sa.case((filtered.c.task_id.is_(None),1),else_=0))).label('tasks'))
        groups=groups.group_by(category).subquery('keyword_groups')
        same=sa.or_(category==groups.c.keyword,sa.and_(category.is_(None),groups.c.keyword.is_(None)))
        observations=l.join(filtered,filtered.c.id==l.c.batch_id)
        count=sa.select(sa.func.count()).select_from(observations).where(same).correlate(groups).scalar_subquery()
        unique=sa.select(sa.func.count(sa.distinct(l.c.product_id))).select_from(observations).where(same).correlate(groups).scalar_subquery()
        statement=sa.select(groups,count.label('count'),unique.label('unique_count'))
        from ecommerce.infrastructure.storage.pagination import numbered_rows
        from ecommerce.infrastructure.storage.sorting import sorted_statement
        ordered,spec=sorted_statement(statement,{'keyword':groups.c.keyword,'captured_at':groups.c.captured_at,'tasks':groups.c.tasks,
            'pages':groups.c.pages,'count':count},groups.c.id,o.get('sort_by'),o.get('order'),'captured_at','desc')
        rows,info=numbered_rows(c,ordered,o.get('page',1))
        return {'keyword_groups':rows,**info,'sort_spec':spec}
    if view == 'searches':
        after, limit = o.get('after', 0), o.get('limit', 30)
        _page(after, limit)
        source = _text(o.get('source', ''))
        statement = sa.select(b.c.task_id, sa.func.min(b.c.id).label('id'), sa.func.min(b.c.captured_at).label('captured_at'),
            sa.func.count().label('pages'),sa.func.min(b.c.search_keyword).label('keyword')).where(visible, b.c.module == 'list')
        selected=o.get('category')
        missing=o.get('category_missing',False)
        if selected is not None: selected=_text(selected)
        def in_category(table):
            """限定当前搜索词；参数：批次表或别名；返回：筛选表达式。"""
            return sa.func.nullif(table.c.search_keyword,'').is_(None) if missing else table.c.search_keyword==selected
        if selected is not None or missing:
            statement=statement.where(in_category(b),*dates)
        if date_bounds:
            matched_date=b.alias('matched_date')
            conditions=[matched_date.c.task_id==b.c.task_id,matched_date.c.visibility=='published',matched_date.c.module=='list']
            if selected is not None or missing: conditions.append(in_category(matched_date))
            if 'since' in date_bounds:
                conditions.append(matched_date.c.captured_at>=date_bounds['since'])
            if 'until' in date_bounds:
                conditions.append(matched_date.c.captured_at<date_bounds['until'])
            statement=statement.where(sa.or_(sa.and_(b.c.task_id.is_(None),*dates),sa.exists(sa.select(matched_date.c.id).where(*conditions))))
        if source:
            matched=b.alias('matched_source')
            statement = statement.where(sa.or_(b.c.search_keyword==source,sa.exists(sa.select(matched.c.id).where(matched.c.task_id==b.c.task_id,
                matched.c.visibility=='published',matched.c.module=='list',matched.c.search_keyword==source))))
        grouped = statement.group_by(b.c.task_id,sa.case((b.c.task_id.is_(None),b.c.id),else_=0)).subquery()
        statement=sa.select(grouped).where(grouped.c.id>after).order_by(grouped.c.id)
        if o.get('page') is not None:
            from ecommerce.infrastructure.storage.pagination import numbered_rows
            from ecommerce.infrastructure.storage.sorting import sorted_statement
            group_scope=sa.and_(visible,b.c.module=='list',sa.or_(b.c.task_id==grouped.c.task_id,
                sa.and_(grouped.c.task_id.is_(None),b.c.id==grouped.c.id)))
            if selected is not None or missing: group_scope=sa.and_(group_scope,in_category(b))
            if selected is not None or missing: group_scope=sa.and_(group_scope,*dates)
            observations=sa.select(sa.func.count()).select_from(l.join(b,b.c.id==l.c.batch_id)).where(group_scope).correlate(grouped).scalar_subquery()
            task_status=sa.select(tasks.c.status).where(tasks.c.id==grouped.c.task_id).correlate(grouped).scalar_subquery()
            ordered,spec=sorted_statement(statement,{'captured_at':grouped.c.captured_at,'keyword':grouped.c.keyword,'pages':grouped.c.pages,
                'count':observations,'status':task_status},
                grouped.c.id,o.get('sort_by'),o.get('order'),'captured_at','desc')
            rows,page_info=numbered_rows(c,ordered,o['page'])
            page_info['sort_spec']=spec
            more=page_info['has_more']
        else:
            rows = [dict(r) for r in c.execute(statement.limit(limit + 1)).mappings()]
            more, rows = len(rows) > limit, rows[:limit]
        for row in rows:
            scope = sa.and_(visible, b.c.module == 'list', b.c.task_id == row['task_id'],
                b.c.id==row['id'] if row['task_id'] is None else sa.true())
            if selected is not None or missing: scope=sa.and_(scope,in_category(b),*dates)
            row['pages']=c.scalar(sa.select(sa.func.count()).select_from(b).where(scope))
            row['captured_at']=c.scalar(sa.select(sa.func.min(b.c.captured_at)).where(scope))
            row['keywords'] = list(c.execute(sa.select(b.c.search_keyword).where(scope).distinct()).scalars())
            row['count'] = c.scalar(sa.select(sa.func.count()).select_from(l.join(b, b.c.id == l.c.batch_id)).where(scope))
            row['unique_count'] = c.scalar(sa.select(sa.func.count(sa.distinct(l.c.product_id))).select_from(l.join(b, b.c.id == l.c.batch_id)).where(scope))
            row['task'] = dict(c.execute(sa.select(tasks).where(tasks.c.id == row['task_id'])).mappings().first() or {})
        return {'searches': rows, 'has_more': more, 'next_cursor': rows[-1]['id'] if rows else after,
            **(page_info if o.get('page') is not None else {})}
    if view=='task':
        if not re.fullmatch(r'[a-f0-9]{32}',o['task_id']):
            raise ValueError('任务编号格式无效')
        task=c.execute(sa.select(tasks).where(tasks.c.id==o['task_id'])).mappings().first()
        if not task:
            raise LookupError('任务不存在')
        return {'task':dict(task)}
    if view in {'search','search_records'}:
        task_id = o['task_id']
        if not re.fullmatch(r'[a-f0-9]{32}', task_id):
            raise ValueError('任务编号格式无效')
        statement=sa.select(b).where(visible,b.c.module=='list',b.c.task_id==task_id,*dates)
        if o.get('category_missing'):
            statement=statement.where(sa.func.nullif(b.c.search_keyword,'').is_(None))
        elif o.get('category') is not None:
            statement=statement.where(b.c.search_keyword==_text(o['category']))
        rows = [dict(r) for r in c.execute(statement.order_by(b.c.page_number,b.c.id)).mappings()]
        if not rows and (view=='search' or not c.scalar(sa.select(tasks.c.id).where(tasks.c.id==task_id))):
            raise LookupError('没有已发布的搜索批次')
        for row in rows:
            row.pop('result_payload', None)
        result={'pages': rows, 'task_id': task_id,
            'task': dict(c.execute(sa.select(tasks).where(tasks.c.id == task_id)).mappings().first() or {})}
        if view=='search_records':
            from ecommerce.infrastructure.storage.search_observations import search_observations
            result['search_pages']=result.pop('pages')
            result.update(search_observations(c,tables,rows,o))
        return result
    if view=='module_skip':
        # 只接受同一任务、商品的最新问答步骤，旧运行和本地进度不能替代数据库证据。
        if not re.fullmatch(r'[a-f0-9]{32}',o.get('task_id','')) or not re.fullmatch(r'[1-9][0-9]{0,24}',o.get('product_id','')):
            raise ValueError('模块跳过查询的任务或商品编号无效')
        runs,steps=tables['collection_runs'],tables['collection_steps']
        latest=c.execute(sa.select(steps).join(runs,runs.c.id==steps.c.run_id).join(p,p.c.id==runs.c.product_id).where(
            runs.c.task_id==o['task_id'],p.c.platform=='taobao',p.c.platform_product_id==o['product_id'],
            steps.c.module=='questions',steps.c.requested.is_(True)).order_by(runs.c.started_at.desc(),runs.c.id.desc(),steps.c.id.desc()).limit(1)).mappings().first()
        record=((latest or {}).get('payload') or {}).get('record') or {}
        search=record.get('entry_search') or {}
        confirmed=bool(latest and latest['status']=='skipped' and latest['continuation_allowed'] and latest['finished_at']
            and record.get('status')=='skipped' and record.get('stop_reason')=='entry_not_found'
            and search.get('max_scrolls')==10 and type(search.get('scroll_count')) is int and 0<=search['scroll_count']<=10
            and (search.get('stop_reason')=='scroll_limit_reached' and search['scroll_count']==10
                 or search.get('stop_reason')=='stable_position' and type(search.get('unchanged_count')) is int and search['unchanged_count']>=3))
        return {'skipped':confirmed,'reason':'entry_not_found' if confirmed else None,
                'message':record.get('message') if confirmed else None}
    if view == 'product':
        product_id = o['product_id']
        if not re.fullmatch(r'[0-9]{1,25}', product_id) or int(product_id) == 0:
            raise ValueError('商品编号格式无效')
        product = c.execute(sa.select(p).where(p.c.platform_product_id == product_id,p.c.platform=='taobao')).mappings().first()
        if not product:
            raise LookupError('商品不存在')
        own = sa.or_(b.c.product_id == product['id'], sa.exists(sa.select(l.c.id).where(l.c.batch_id == b.c.id, l.c.product_id == product['id'])))
        history = [dict(r) for r in c.execute(sa.select(b).where(visible, own).order_by(b.c.captured_at.desc(), b.c.id.desc())).mappings()]
        attach_batch_counts(c,tables,history)
        for row in history:
            row.pop('result_payload', None)
        pointers = list(c.execute(sa.select(current.c.batch_id).join(b, b.c.id == current.c.batch_id).where(
            current.c.product_id == product['id'], visible)).scalars())
        listing = c.execute(sa.select(l,b.c.captured_at).join(b, b.c.id == l.c.batch_id).where(l.c.product_id == product['id'], visible)
            .order_by(b.c.captured_at.desc(), l.c.id.desc()).limit(1)).mappings().first()
        from ecommerce.infrastructure.storage.collection_context import detail_summary,observation_context
        summary=detail_summary(c,tables,product['id'])
        context=None
        if o.get('observation_id'):
            observation=c.execute(sa.select(l,b.c.search_keyword,b.c.page_number,b.c.captured_at.label('list_captured_at')).join(b,b.c.id==l.c.batch_id).where(
                l.c.id==o['observation_id'],l.c.product_id==product['id'],visible)).mappings().first()
            if not observation: raise LookupError('列表来源与商品不匹配')
            context={**observation_context(c,tables,dict(observation),summary),'observation':dict(observation)}
        sku_query=sa.select(b.c.id,b.c.captured_at,b.c.result_payload).where(visible,b.c.product_id==product['id'],b.c.module=='basic_info',b.c.status=='complete')
        context_run=(context or {}).get('context_run',{}).get('id')
        sku_query=sku_query.where(b.c.run_id==context_run) if context_run else sku_query.where(b.c.id.in_(pointers))
        sku=c.execute(sku_query.order_by(b.c.captured_at.desc(),b.c.id.desc()).limit(1)).mappings().first()
        info={}
        if sku:
            basic_table=tables['product_info_observations']
            observed=c.execute(sa.select(basic_table).where(basic_table.c.batch_id==sku['id'])).mappings().first()
            info=dict(observed or {})
            if observed:
                info={**(observed['payload'] or {}),**info}
        return {'product': dict(product), 'history': history, 'current_ids': pointers, 'listing': dict(listing or {}),
            'detail_summary':summary,'source_context':context,'product_info':info,'info_captured_at':info.get('captured_at'), 'info_batch_id':sku['id'] if sku else None}
    if view=='sources':
        product=c.execute(sa.select(p).where(p.c.platform_product_id==o['product_id'],p.c.platform=='taobao')).mappings().first()
        if not product: raise LookupError('商品不存在')
        from ecommerce.infrastructure.storage.pagination import numbered_rows
        from ecommerce.infrastructure.storage.sorting import sorted_statement
        statement=sa.select(l,b.c.task_id,b.c.search_keyword,b.c.page_number,b.c.captured_at).join(b,b.c.id==l.c.batch_id).where(l.c.product_id==product['id'],visible)
        ordered,spec=sorted_statement(statement,{'captured_at':b.c.captured_at,'keyword':b.c.search_keyword,'position':l.c.slot_index},l.c.id,
            o.get('sort_by'),o.get('order'),'captured_at','desc')
        rows,info=numbered_rows(c,ordered,o.get('page',1))
        from ecommerce.shared.pagination import PAGE_SIZE
        for row in rows:
            preceding=c.scalar(sa.select(sa.func.count()).select_from(l).where(l.c.batch_id==row['batch_id'],
                sa.or_(l.c.slot_index<row['slot_index'],sa.and_(l.c.slot_index==row['slot_index'],l.c.id<row['id']))))
            row['list_page']=preceding//PAGE_SIZE+1
        return {'sources':rows,**info,'sort_spec':spec}
    raise ValueError('不支持的资料视图')
