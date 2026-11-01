"""SKU更新界面的站内链接及北京时间文本。"""
import re
from flask import current_app, url_for


def present(model,product_id):
    """补齐更新概况显示；参数：业务模型和平台商品编号；返回：新界面模型。"""
    import copy
    model=copy.deepcopy(model)
    for batch in (model['latest_valid'],model['latest_attempt'],(model.get('outcome') or {}).get('batch')):
        if batch:
            batch['time_text']=current_app.jinja_env.filters['full_local_time'](batch['captured_at'])
            batch['url']=url_for('pages.collection_product',product_id=product_id,batch=batch['id'],module='skus')
    outcome=model.get('outcome')
    if outcome:
        task=outcome.get('task_id');valid_task=isinstance(task,str) and re.fullmatch('[a-f0-9]{32}',task)
        outcome['task_url']=url_for('pages.task_detail',task_id=task) if valid_task else None
        outcome['logs_url']=url_for('pages.logs',task_id=task) if valid_task else None
    model['queue_url']=url_for('pages.module',key='collection',input=product_id,mode='detail',modules='skus',jobs_status='waiting')
    return model
