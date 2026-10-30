"""仅在内存回放已保存响应；不连接数据库、不创建AI客户端、不恢复旧任务。"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
from ecommerce.modules.review_analysis import v4,v5
from tests.unit.test_review_analysis_v5 import unpack


def replay(reference,output):
    """核对真实失败形状及完整发送边界；参数：只读本地快照、证据路径；返回：摘要。"""
    original=reference.read_bytes();data=json.loads(original);current=copy.deepcopy(data)
    old_size=len(v4.prompt(data['task']['rules'],'report',v4.context(data)))
    current['details']=[];current['reports']=[];current['report']=None
    current['task']['rules']=v5.snapshot();current['task']['rule_version']=v5.VERSION
    current['task']['parameters']['validation_history']=[]
    sources={s['review_id']:s for s in current['sources']};merged=[];diagnostics=[]
    for call in current['calls']:
        if call['kind']!='classify':continue
        selected=[sources[uid]['snapshot'] for uid in call['request']['source_ids']]
        assessment=v5.assess(call['response']['text'],selected,call['response']['finish_reason'])
        assert not assessment['technical_failure']
        current['task']['parameters']['validation_history'].append({'policy':v5.POLICY,'outcomes':assessment['outcomes']})
        for row in assessment['results']:
            current['details'].append({'id':'offline-'+str(row['id']),'source_id':sources[row['id']]['id'],'result':row,'revision':0,'corrections':[]})
        merged.extend(i['review_id'] for i in assessment['outcomes'] if i['merged_fragments'])
        diagnostics.extend(assessment['issues'])
    incoming=v5.context(current);sent=v5.wire(incoming);restored=unpack(sent)
    lookup={r['id']:r for r in restored['extraction']['reviews']}
    for expected in incoming['extraction']['reviews']:
        actual=lookup[expected['id']]
        for key in ('evidence','usage_stages','relations','flags'):assert expected[key]==actual[key],(expected['id'],key)
        for before,after in zip(expected['observations'],actual['observations']):
            for key in v5.contract.OBSERVATION['properties']:assert before[key]==after[key],(expected['id'],key)
    new_size=len(v5.prompt(v5.snapshot(),'report',incoming))
    assert new_size<=v5.MAX_INPUT
    content=v5.content(current);assert sum(content['coverage']['state_counts'].values())==len(sources)
    assert reference.read_bytes()==original
    report={'read_only_reference_task':data['task']['id'],'reference_sha256':hashlib.sha256(original).hexdigest(),
        'source_count':len(sources),'original_saved_count':len(data['details']),'in_memory_processed_count':len(current['details']),
        'merged_source_ids':merged,'state_counts':content['coverage']['state_counts'],
        'original_full_prompt_characters':old_size,'new_full_prompt_characters':new_size,'limit_characters':v5.MAX_INPUT,
        'all_observation_and_quote_fields_roundtrip':True,'observations_sent':len(sent['observations']),'quotes_sent':len(sent['evidence']),
        'real_ai_calls':0,'database_connections':0,'restored_old_task':False,'reference_unchanged':True}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--reference',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();replay(args.reference,args.output)
