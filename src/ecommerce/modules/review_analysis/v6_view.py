"""新报告读取与安全渲染，查看和导出不创建AI客户端。"""
import copy
from ecommerce.modules.review_analysis import v6_report,v6_runtime,v6_checks


def enrich(settings,data,version=None):
    """选择报告版本并派生显示；参数：配置、聚合、可选版本；返回：读模型。"""
    if version is not None:
        selected=next((r for r in data['reports'] if r['version']==version),None)
        if selected is None:raise LookupError('报告版本不存在。')
        data['report']=selected
    content=copy.deepcopy(data['report']['content']) if data['report'] else v6_report.content(data)
    data['legacy_inspection']=not content.get('inspection')
    if data['legacy_inspection']:
        # 旧版本只补齐显示字段，不用新规则覆盖当时保存的引用结果，也不写库。
        final=next((c['response'] for c in reversed(data['calls']) if c['chunk_index']==0 and c['response'] and c['response']['text']==content['markdown']),None)
        content['generation']=v6_checks.generation(content['markdown'],final['finish_reason'] if final else None)
        if content['origin']=='local_empty':content['generation'].update(state='insufficient_information',complete=True,issues=[])
        entries=content['validation']['citations'];valid=sum(c['valid'] for c in entries)
        content['validation']['counts']={'citation_count':len(entries),'valid_count':valid,'pending_count':len(entries)-valid,
            'invalid_count':len(entries)-valid,'unrecognized_count':0,'valid_source_count':content['coverage']['referenced_count']}
        for index,entry in enumerate(entries,1):
            entry.update(index=index,reason='此处保留旧规则检查结果，请打开最新版本后本地重新检查。',
                original_text=None,marker='旧版引用')
    content['html']=v6_report.render(content['markdown'])
    for phase in content['phases']:phase['html']=v6_report.render(phase['text'])
    data['v6_content']=content
    data['is_latest_report']=bool(data['report'] and data['reports'] and data['report']['id']==data['reports'][-1]['id'])
    data['needs_local_recovery']=any(c['status']=='inflight' for c in data['calls'])
    data['display_message']=data['task']['message'] if data['task']['status'] in {'queued','running'} else v6_report.message(content)
    if data['legacy_inspection']:data['display_message']='报告正文已完整返回；此版本保存的是旧规则检查结果，请本地重新检查引用。' if content['generation']['complete'] else '此版本保存的是旧规则检查结果；'+data['display_message']
    try:data['resume_budget']=v6_runtime.remaining_budget(data)
    except ValueError as error:data['resume_budget']={'blocked':str(error),'required_calls':0}
    data['outgoing']=data['task']['parameters']['plan']['groups']
    return data
