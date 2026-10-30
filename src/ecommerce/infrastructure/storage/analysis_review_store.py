"""通用报告逐项人工决定：任务行锁、报告归属、内容绑定和追加记录。"""
from uuid import uuid4
import re
import sqlalchemy as sa
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.infrastructure.storage.time_values import database_now


def decisions(settings,analysis_id,report_id):
    """读取指定历史报告的决定；参数：配置、分析和报告；返回：按追加序号排列的记录。"""
    with repository.connection(settings) as (conn,tables):
        t=tables['analysis_review_decisions']
        return [dict(r) for r in conn.execute(sa.select(t).where(t.c.analysis_id==analysis_id,t.c.report_id==report_id).order_by(t.c.created_at,t.c.revision,t.c.id)).mappings()]


def save(settings,analysis_id,*,report_id,item_key,candidate_hash,source_hash,expected_revision,decision,reviewer,reason,review_context=None):
    """保存明确人工选择；参数：报告内容身份和旧序号、决定及意见；返回：新序号。"""
    if decision not in ('pending','adopted','rejected') or type(expected_revision) is not int or expected_revision<0:
        raise ValueError('复核状态或版本无效。')
    if not isinstance(reviewer,str) or not 1<=len(reviewer.strip())<=80 or not isinstance(reason,str) or not 1<=len(reason.strip())<=1000:
        raise ValueError('请填写复核人及1～1000字意见。')
    if not all(isinstance(value,str) and re.fullmatch('[a-f0-9]{'+str(size)+'}',value) for value,size in ((analysis_id,32),(report_id,32),(candidate_hash,64),(source_hash,64))) or not isinstance(item_key,str) or not re.fullmatch('[a-z0-9:_-]{1,160}',item_key):
        raise ValueError('复核内容或来源标识无效。')
    review_context={} if review_context is None else review_context
    if not isinstance(review_context,dict) or set(review_context)-{'evidence_checked','conditions_checked','stop_scope'}:raise ValueError('复核声明无效。')
    if decision=='adopted' and review_context.get('evidence_checked') is not True:raise ValueError('采用前请明确核对所引依据。')
    if decision=='adopted' and item_key.startswith('opportunity:') and (review_context.get('conditions_checked') is not True or review_context.get('stop_scope') not in ('current_model','supplier','bundle','product_direction')):
        raise ValueError('采用方向前请核对问题对应的动作和继续条件，并明确淘汰范围。')
    with repository.connection(settings,True) as (conn,tables):
        task=repository.task_row(conn,tables,analysis_id,True)
        if task['status'] in ('queued','running'):raise ValueError('运行期间不能保存复核决定。')
        r=tables['analysis_reports'];d=tables['analysis_details'];t=tables['analysis_review_decisions']
        report=conn.execute(sa.select(r).where(r.c.id==report_id,r.c.analysis_id==analysis_id)).mappings().one_or_none()
        latest=conn.scalar(sa.select(sa.func.max(r.c.version)).where(r.c.analysis_id==analysis_id))
        if report is None or report['version']!=latest:raise ValueError('仅可复核当前报告版本；旧版本可查看，请刷新后操作。')
        content=report['content']
        revisions={row.id:row.revision for row in conn.execute(sa.select(d.c.id,d.c.revision).where(d.c.analysis_id==analysis_id))}
        if revisions!=content['detail_revisions']:raise ValueError('明细已变化，请先重新统计再复核。')
        source_rows=tables['analysis_sources']
        actual_source=fingerprint({'sources':[(s.id,s.content_hash) for s in conn.execute(sa.select(source_rows.c.id,source_rows.c.content_hash).where(source_rows.c.analysis_id==analysis_id).order_by(source_rows.c.position))], 'details':revisions})
        item=next((i for i in content.get('review_items',[]) if i['key']==item_key),None)
        if not item or fingerprint(item['candidate'])!=candidate_hash or item['candidate_hash']!=candidate_hash or source_hash!=content.get('source_hash') or actual_source!=source_hash:
            raise ValueError('复核内容或来源已变化，请刷新后核对。')
        if decision=='adopted' and item['kind']=='选品方向' and (review_context.get('conditions_checked') is not True or review_context.get('stop_scope') not in ('current_model','supplier','bundle','product_direction')):
            raise ValueError('采用方向前请核对问题对应的动作和继续条件，并明确淘汰范围。')
        revision=conn.scalar(sa.select(sa.func.max(t.c.revision)).where(t.c.report_id==report_id,t.c.item_key==item_key)) or 0
        if revision!=expected_revision:raise ValueError('该条目的审核决定已变化，请刷新后重试。')
        conn.execute(t.insert().values(id=uuid4().hex,analysis_id=analysis_id,report_id=report_id,item_key=item_key,
            candidate_hash=candidate_hash,source_hash=source_hash,revision=revision+1,decision=decision,
            reviewer=reviewer.strip(),reason=reason.strip(),review_context=review_context if decision=='adopted' else {},created_at=database_now()))
        return {'revision':revision+1,'decision':decision}
