"""问答分析来源读取：整批读取并保持问题回答归属。"""
import sqlalchemy as sa


def sources(conn,tables,product,batch_id):
    """读取已发布整批问答；参数：事务、模型、平台商品、批次；返回：身份、批次、脱敏快照。"""
    p,b,q,a=(tables[n] for n in ('products','collection_batches','question_observations','answer_observations'))
    identity=conn.execute(sa.select(p).where(p.c.platform=='taobao',p.c.platform_product_id==product)).mappings().one_or_none()
    if not identity:raise LookupError('商品尚未入库。')
    batch=conn.execute(sa.select(b).where(b.c.id==batch_id,b.c.product_id==identity['id'],b.c.module=='questions',
        b.c.visibility=='published',b.c.status.in_(['complete','no_questions']))).mappings().one_or_none()
    if not batch:raise LookupError('请选择该商品已完成、已发布的问大家批次。')
    questions=conn.execute(sa.select(q).where(q.c.batch_id==batch_id).order_by(q.c.position,q.c.id)).mappings().all()
    answers=conn.execute(sa.select(a).where(a.c.batch_id==batch_id).order_by(a.c.position,a.c.id)).mappings().all()
    grouped={r['id']:[] for r in questions}
    for answer in answers:
        if answer['question_id'] not in grouped:raise ValueError('回答与本批问题归属不一致。')
        item={'id':answer['id'],'text':answer['content'] or ''}
        for field,key in [('sku_text','sku'),('date_raw','date')]:
            if answer.get(field):item[key]=answer[field]
        for key in ('identity_labels','purchase_labels'):
            value=(answer.get('payload') or {}).get(key)
            if isinstance(value,list) and all(isinstance(v,str) for v in value) and value:item[key]=value
        grouped[answer['question_id']].append(item)
    items=[]
    for row in questions:
        item={'id':row['id'],'text':row['question'] or '', 'answers':grouped[row['id']],
            'answers_complete':bool(row['answers_complete']),'answer_stop_reason':row['answer_stop_reason']}
        if row.get('date_raw'):item['date']=row['date_raw']
        items.append(item)
    if not items:raise ValueError('本批没有已保存的问题，无需调用AI。')
    return dict(identity),dict(batch),items
