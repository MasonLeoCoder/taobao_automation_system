"""只读采集资料与来源关系，商品关联不替代本次采集入口关系。"""
import sqlalchemy as sa

DETAIL_MODULES = {'basic_info','video','main_images','skus','parameters','detail_images','review_summary','reviews','questions'}
RUN_LABELS = {'complete_with_errors':'已结束，存在模块错误','running':'执行中','complete':'所选模块完成','complete_with_warnings':'所选模块完成（含警告）',
    'partial':'部分完成','failed':'失败','needs_attention':'待人工处理','interrupted':'已中断','cancelled':'已取消'}


def detail_summary(connection,tables,product_id):
    """查询独立详情资料情况；参数：连接、表和内部商品编号；返回：资料及最后尝试摘要。"""
    b,cur,r,e=[tables[k] for k in ('collection_batches','product_current_sections','collection_runs','search_records')]
    published=sa.and_(b.c.product_id==product_id,b.c.module.in_(DETAIL_MODULES),b.c.visibility=='published')
    modules=list(connection.execute(sa.select(b.c.module).where(published).distinct()).scalars())
    valid=list(connection.execute(sa.select(cur.c.module).join(b,b.c.id==cur.c.batch_id).where(
        cur.c.product_id==product_id,published).distinct()).scalars())
    latest=connection.execute(sa.select(r).where(r.c.product_id==product_id).order_by(r.c.started_at.desc(),r.c.id.desc()).limit(1)).mappings().first()
    entry=connection.execute(sa.select(e.c.captured_at).where(e.c.product_id==product_id,e.c.record_type=='detail_open')
        .order_by(e.c.captured_at.desc(),e.c.id.desc()).limit(1)).first()
    captured=connection.scalar(sa.select(sa.func.max(b.c.captured_at)).where(published))
    label=('九模块已有有效资料（各自范围）' if len(valid)==9 else '部分模块已有有效资料' if valid else
        '仅有采集尝试，无有效分区' if modules else '仅有详情入口，尚无保存内容' if entry else
        '尚无已保存详情内容' if latest else '暂无详情资料')
    return {'modules':modules,'valid_modules':valid,'label':label,'has_content':bool(modules),
        'has_detail_activity':bool(modules or entry or latest),'latest_run':dict(latest or {}),
        'last_attempt_label':RUN_LABELS.get(latest['status'],latest['status']) if latest else '未记录详情运行',
        'captured_at':captured,'last_attempt_at':latest['started_at'] if latest else captured or (entry[0] if entry else None)}


def observation_context(connection,tables,observation,summary=None):
    """核对某次列表位置的详情入口；参数：连接、表、观察和可选资料摘要；返回：本次关联。"""
    r,e,b=[tables[k] for k in ('collection_runs','search_records','collection_batches')]
    batch=connection.execute(sa.select(b.c.task_id).where(b.c.id==observation['batch_id'])).first()
    product_id=observation.get('product_id')
    if not product_id:
        return {'detail_summary':None,'relation':'none','relation_label':'商品身份未确认','context_run':{}}
    summary=summary or detail_summary(connection,tables,product_id)
    exact=connection.execute(sa.select(r).join(e,e.c.id==r.c.entry_record_id).where(
        r.c.product_id==product_id,e.c.product_id==product_id,e.c.source_list_observation_id==observation['id'])
        .order_by(r.c.started_at.desc(),r.c.id.desc()).limit(1)).mappings().first()
    shared=None
    if not exact and batch and batch[0]:
        shared=connection.execute(sa.select(r).where(r.c.product_id==product_id,r.c.task_id==batch[0])
            .order_by(r.c.started_at.desc(),r.c.id.desc()).limit(1)).mappings().first()
    run=exact or shared
    relation='entry' if exact else 'same_task' if shared else 'other' if summary['has_detail_activity'] else 'none'
    label={'entry':'本位置是详情采集入口','same_task':'同任务已有详情，本位置非确认入口',
        'other':'本次未关联详情，可查看其他时间资料','none':'本次未关联详情，暂无详情资料'}[relation]
    return {'detail_summary':summary,'relation':relation,'relation_label':label,'context_run':dict(run or {}),
        'execution_label':RUN_LABELS.get(run['status'],run['status']) if run else '未关联详情运行'}
