"""采集概况的站内链接和时间格式，业务状态由只读模型决定。"""
from flask import current_app,url_for
from ecommerce.web.batch_presentation import batch_scope_text


def present(model,product_id):
    """补充界面文本与链接；参数：概况、商品编号；返回：可直接渲染的模型。"""
    from ecommerce.infrastructure.storage.collection_queries import public
    model=public(model)
    time=current_app.jinja_env.filters['full_local_time']
    back=url_for('pages.collection_product',product_id=product_id,module='overview')
    for row in model['rows']:
        source=row.pop('scope_batch',None)
        row['scope']=batch_scope_text(dict(source,record_counts={})) if source else ''
        row['captured_text']=time(row['captured_at']);row['attempt_time']=time(row['attempt_at'])
        row['source_url']=url_for('pages.collection_product',product_id=product_id,batch=row['source_batch'],return_to=back) if row['source_batch'] else None
        row['source_logs']=url_for('pages.logs',task_id=row['source_task'],return_to=back) if row['source_task'] else None
        row['attempt_logs']=url_for('pages.logs',task_id=row['attempt_task'],return_to=back) if row['attempt_task'] and row['attempt_task']!=row['source_task'] else None
        row['attempt_task_url']=url_for('pages.task_detail',task_id=row['attempt_task'],return_to=back) if row['attempt_logs'] and not row['busy'] else None
        row['configure_url']=url_for('pages.module',key='collection',input=product_id,mode='detail',modules=row['module'])
        row['active_url']=(url_for('pages.task_detail',task_id=row['attempt_task']) if row['attempt_task'] else
                           url_for('pages.module',key='collection',input=product_id,mode='detail',modules=row['module'],jobs_status='waiting')) if row['busy'] else None
        row['active_label']='查看当前任务' if row['attempt_task'] else '查看申请进度'
    return model
