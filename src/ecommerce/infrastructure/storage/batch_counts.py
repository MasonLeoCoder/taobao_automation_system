"""按批次汇总已存记录；仅供查询展示，不修改完成状态或覆盖范围。"""
import sqlalchemy as sa


def attach_batch_counts(connection, tables, batches):
    """补充批次实际数量；参数：连接、表集合、批次列表；返回：原位附加 record_counts。"""
    groups={}
    by_id={batch['id']:batch for batch in batches}
    for batch in batches:
        batch['record_counts']={}
        groups.setdefault(batch['module'],[]).append(batch['id'])
    observations={'basic_info':'product_info_observations','list':'list_observations',
        'parameters':'parameter_observations','skus':'sku_observations',
        'reviews':'review_observations','questions':'question_observations'}
    for module,table_name in observations.items():
        identifiers=groups.get(module)
        if not identifiers:
            continue
        table=tables[table_name]
        columns=[table.c.batch_id,sa.func.count().label('records')]
        if module=='skus':
            columns.append(sa.func.sum(sa.case((table.c.stock_status=='out_of_stock',1),else_=0)).label('out_of_stock'))
        for row in connection.execute(sa.select(*columns).where(table.c.batch_id.in_(identifiers))
                .group_by(table.c.batch_id)).mappings():
            by_id[row['batch_id']]['record_counts'].update({key:int(value) for key,value in row.items() if key!='batch_id'})
    for module,role in {'video':'main_video','main_images':'main_image','detail_images':'detail_image'}.items():
        identifiers=groups.get(module)
        if identifiers:
            table=tables['media_references']
            for identifier,count in connection.execute(sa.select(table.c.batch_id,sa.func.count()).where(
                    table.c.batch_id.in_(identifiers),table.c.role==role).group_by(table.c.batch_id)):
                by_id[identifier]['record_counts']['records']=count
    if groups.get('review_summary'):
        table=tables['review_summaries']
        for identifier,tags in connection.execute(sa.select(table.c.batch_id,table.c.tags).where(
                table.c.batch_id.in_(groups['review_summary']))):
            if isinstance(tags,list):
                by_id[identifier]['record_counts']['tags']=len(tags)
    if groups.get('questions'):
        questions,answers=tables['question_observations'],tables['answer_observations']
        statement=sa.select(questions.c.batch_id,sa.func.count(answers.c.id)).select_from(questions).outerjoin(
            answers,sa.and_(answers.c.question_id==questions.c.id,answers.c.batch_id==questions.c.batch_id)).where(
            questions.c.batch_id.in_(groups['questions'])).group_by(questions.c.batch_id)
        for identifier,count in connection.execute(statement):
            by_id[identifier]['record_counts']['answers']=count
