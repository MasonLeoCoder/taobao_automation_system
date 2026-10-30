"""新报告读取与安全渲染，查看和导出不创建AI客户端。"""
import copy
from ecommerce.modules.review_analysis import v6_report,v6_runtime


def enrich(settings,data,version=None):
    """选择报告版本并派生显示；参数：配置、聚合、可选版本；返回：读模型。"""
    if version is not None:
        selected=next((r for r in data['reports'] if r['version']==version),None)
        if selected is None:raise LookupError('报告版本不存在。')
        data['report']=selected
    content=copy.deepcopy(data['report']['content']) if data['report'] else v6_report.content(data)
    content['html']=v6_report.render(content['markdown'])
    for phase in content['phases']:phase['html']=v6_report.render(phase['text'])
    data['v6_content']=content
    data['is_latest_report']=bool(data['report'] and data['reports'] and data['report']['id']==data['reports'][-1]['id'])
    data['needs_local_recovery']=any(c['status']=='inflight' for c in data['calls'])
    try:data['resume_budget']=v6_runtime.remaining_budget(data)
    except ValueError as error:data['resume_budget']={'blocked':str(error),'required_calls':0}
    data['outgoing']=data['task']['parameters']['plan']['groups']
    return data
