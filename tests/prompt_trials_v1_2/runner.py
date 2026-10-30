"""显式四次联网的独立V1.2试验入口；先离线检查，逐阶段调用，正式数据库只读。"""
import argparse
import copy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo
import sqlalchemy as sa
from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.ai.config import load_config
from ecommerce.infrastructure.ai.client import create_client, AIError
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.mysql import create_mysql_engine, current_revision
from ecommerce.infrastructure.storage.mysql_migrations.schema_v13 import build_metadata
from ecommerce.infrastructure.tasks.lock import shop_lock
from tests.prompt_trials_v1_2 import contracts, processing

PROJECT=Path(__file__).resolve().parents[2]
PHASES={'real_extract':24576,'synthetic_extract':16384,'complete_report':16384,'partial_report':16384}
LIMIT=400000


def now():
    """返回北京时间；参数：无；返回：ISO文字。"""
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def encode(value):
    """紧凑序列化；参数：对象；返回：JSON文字。"""
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),default=str)


def write(path, value):
    """原子保存新证据；参数：路径和值；返回：无。"""
    path=Path(path);temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8');temporary.replace(path)


def load(path):
    """读取JSON；参数：路径；返回：对象。"""
    return json.loads(Path(path).read_text(encoding='utf-8'))


def checksum(path):
    """计算文件SHA256；参数：路径；返回：哈希。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_hashes():
    """固定实际受测核心程序；参数：无；返回：文件名与指纹。"""
    return {name:checksum(Path(__file__).parent/name) for name in ('contracts.py','processing.py','runner.py','test_offline.py')}


def snapshot():
    """逐表只读取证两库；参数：无；返回：行数、内容指纹和版本。"""
    settings=load_settings();result={}
    for database in (settings.mysql.database,'taobao_automation_verify'):
        engine=create_mysql_engine(replace(settings.mysql,database=database))
        try:
            with engine.connect() as conn:
                tables={}
                for name,table in build_metadata().tables.items():
                    rows=[dict(row) for row in conn.execute(sa.select(table).order_by(*table.primary_key.columns)).mappings()]
                    tables[name]={'count':len(rows),'hash':hashlib.sha256(encode(rows).encode()).hexdigest()}
                result[database]={'revision':current_revision(conn),'tables':tables}
        finally:engine.dispose()
    return result


def export():
    """导出冻结版本的完整契约；参数：无；返回：无。"""
    base=PROJECT/'docs/操作参考/评价提示词V1.2'
    for label,schema in contracts.documents().items():write(base/(label+'.schema.json'),schema)


def prepare(baseline):
    """复制同一组样例、固定本轮提示及上限；参数：V1目录；返回：新目录。"""
    baseline=Path(baseline).resolve()
    root=PROJECT/'workspace/output/reports/review_prompt_v1_2/20261030'/(datetime.now().strftime('%H%M%S')+'-'+uuid4().hex[:8])
    root.mkdir(parents=True)
    source=load(baseline/'real-input.json');fake=load(baseline/'synthetic-input.json')
    current=repository.preview(load_settings(),'1050158363561',96)
    ids={r['id'] for r in source['reviews']}
    actual=[r for r in current['items'] if r['id'] in ids]
    if actual!=source['reviews']:raise ValueError('真实来源已变化，未开始试验')
    for name,value in [('real-input.json',source),('synthetic-input.json',fake)]:write(root/name,value)
    prompt_hashes={}
    for label,name in [('extract','提取'),('report','报告')]:
        text=(PROJECT/f'docs/操作参考/评价提示词V1.2/{name}.md').read_text(encoding='utf-8').split('```text\n',1)[1].rsplit('```',1)[0].strip()
        (root/(label+'-instruction.txt')).write_text(text,encoding='utf-8')
        prompt_hashes[label]=checksum(root/(label+'-instruction.txt'))
    for label,schema in contracts.documents().items():write(root/(label+'.schema.json'),schema)
    write(root/'manifest.json',{'trial_id':root.name,'created_at':now(),'baseline':str(baseline),'version':'V1.2',
        'max_calls':4,'max_reserved_tokens':LIMIT,'model':'deepseek-flash','phases':PHASES,
        'real_ids':[r['id'] for r in source['reviews']],'synthetic_ids':[r['id'] for r in fake['reviews']],
        'prompt_hashes':prompt_hashes,'schema_hashes':{label:checksum(root/(label+'.schema.json')) for label in contracts.documents()},
        'approval':'用户明确批准V1.2集中修改；离线通过后同样例最多4次复测；不自动追加版本、费用、重试或正式接入'})
    write(root/'database-before.json',snapshot())
    write(root/'baseline-reference-hashes.json',{'trial':{str(p):checksum(p) for p in baseline.iterdir() if p.is_file()},
        'frozen_files':{str(p):checksum(p) for base in (PROJECT/'tests/prompt_trials',PROJECT/'docs/操作参考/评价提示词V1.1')
                        for p in base.iterdir() if p.is_file()}})
    print(str(root));return root


def offline(root):
    """运行真实离线回归并落盘门禁；参数：目录；返回：结果，不联网。"""
    if (root/'offline-result.json').exists() or list(root.glob('*-intent.json')):
        raise ValueError('本轮离线门禁已固定，不覆盖历史日志；修订需另建目录')
    completed=subprocess.run([sys.executable,'-m','unittest','tests.prompt_trials_v1_2.test_offline','-v'],cwd=PROJECT,
        capture_output=True,text=True,encoding='utf-8',errors='replace')
    log=completed.stdout+completed.stderr
    (root/'offline-tests.log').write_text(log,encoding='utf-8')
    result={'passed':completed.returncode==0,'code_hashes':code_hashes(),'log_sha256':checksum(root/'offline-tests.log'),
            'test_count':int(re.search(r'Ran (\d+) tests',log).group(1)) if re.search(r'Ran (\d+) tests',log) else 0}
    write(root/'offline-result.json',result)
    print(log);return result


def build_report_input(root, partial=False):
    """生成同一样例的完整或阶段输入，统计由程序产生；参数：目录与模拟标记；返回：输入。"""
    source=load(root/'real-input.json');extracted=load(root/'real_extract-processed.json')
    removed={658,672,679} if partial else set()
    rows=[copy.deepcopy(r) for r in extracted['reviews'] if r['id'] not in removed and r['processing_status']!='pending']
    for row in rows:
        for o in row['observations']:o['eligibility'].pop('semantic_review',None)
    stats=processing.statistics(rows)
    stats_for_input=[{k:v for k,v in s.items() if k in ('id','kind','label','event_scope','count','review_ids','observation_refs','uncertain_fields')} for s in stats['statistics']]
    done={r['id'] for r in rows};pending=[r['id'] for r in source['reviews'] if r['id'] not in done]
    scope={'product_id':source['product_context']['platform_product_id'],'batch_ids':[96],'captured_at':source['captured_at'],
           'sample_count':len(source['reviews']),'analyzed_count':len(rows),'pending_count':len(pending),
           'report_stage':'阶段报告' if pending else '完整样例报告',
           'limitations':['目的性选择16条，原批次100条达到采集上限，均不代表平台全部评价或市场需求；编号是评价记录，不是独立买家。',
               '同义主题未自动合并；确定维度可统计，未知字段单独限制；引用及程序计数不等于语义已人工验证。']}
    pending_issues=[{'review_id':uid,'simulation':uid in removed,'issue':'模拟暂不纳入，尺寸信息可能不完整；不得补写其原文。' if uid in removed else '没有形成可用提取，保留待处理状态；不能认定买家文字无法理解。'} for uid in pending]
    def compact(item):
        """只省略空字段，不删文字、零值或布尔值；参数：派生对象；返回：紧凑视图。"""
        if isinstance(item,dict):return {k:compact(v) for k,v in item.items() if v is not None and v!=[] and v!={}}
        if isinstance(item,list):return [compact(v) for v in item]
        return item
    packed=[]
    for row in rows:
        clean=compact(row)
        clean['evidence']=copy.deepcopy(row['evidence'])
        clean['observations']=[compact(o) for o in row['observations']]
        packed.append(clean)
    return {'trial_type':'阶段报告模拟' if partial else '完整样例验证，非原批次整批报告','scope':scope,
            'projection_format':'已校验提取的紧凑视图：仅省略null/空集合，未删任何非空观点或依据；省略字段表示未提供内容。原提取完整结构另行保留。',
            'product_context':source['product_context'],'extraction':{'reviews':packed},'statistics':stats_for_input,
            'statistic_exclusions':stats['excluded_observations'],'pending_issues':pending_issues,'materials':[]}


def reserve(root, phase, text):
    """先核对门禁和固定额度再独占请求名额；参数：目录、阶段、文字；返回：预约记录。"""
    if phase not in PHASES:raise ValueError('阶段不合法')
    manifest=load(root/'manifest.json');gate=load(root/'offline-result.json')
    if not gate['passed'] or gate['test_count']==0 or gate['code_hashes']!=code_hashes() or gate['log_sha256']!=checksum(root/'offline-tests.log'):
        raise ValueError('离线检查未通过或受测代码已改变，未联网')
    if manifest['max_calls']!=4 or manifest['max_reserved_tokens']!=LIMIT or manifest['phases']!=PHASES:raise ValueError('本轮范围发生变化')
    for label,expected in manifest['prompt_hashes'].items():
        if checksum(root/(label+'-instruction.txt'))!=expected:raise ValueError('提示快照已变化')
    for label,expected in manifest['schema_hashes'].items():
        if checksum(root/(label+'.schema.json'))!=expected:raise ValueError('结构快照已变化')
    if not text.strip() or len(text)>60000:raise ValueError('输入超过客户端字符边界，未联网')
    cost=len(text.encode('utf-8'))+PHASES[phase]+256
    with shop_lock(root/'dispatch'):
        prior=[load(p) for p in root.glob('*-intent.json')]
        if (root/(phase+'-intent.json')).exists():raise ValueError('该阶段已预约，禁止重试')
        if len(prior)>=4 or sum(x['reserved_tokens'] for x in prior)+cost>LIMIT:raise ValueError('超过本轮次数或预留上限')
        item={'phase':phase,'reserved_at':now(),'reserved_tokens':cost,'max_output_tokens':PHASES[phase],
              'request_sha256':hashlib.sha256(text.encode()).hexdigest()}
        with (root/(phase+'-intent.json')).open('x',encoding='utf-8') as stream:json.dump(item,stream,ensure_ascii=False,indent=2)
    return item


def call(root, phase, allow_paid=False):
    """显式发送一个阶段，不重试；参数：目录、阶段、费用授权；返回：保存结果。"""
    if not allow_paid:raise ValueError('必须显式传入本轮已获准费用参数')
    if phase not in PHASES:raise ValueError('阶段不合法')
    extracting=phase.endswith('extract')
    if extracting:
        incoming=load(root/('real-input.json' if phase=='real_extract' else 'synthetic-input.json'))
    else:incoming=build_report_input(root,phase=='partial_report')
    label='extract' if extracting else 'report';contract='extraction' if extracting else 'report'
    instruction=(root/(label+'-instruction.txt')).read_text(encoding='utf-8')
    text=instruction+'\n\n完整机器输出结构：\n'+encode(load(root/(contract+'.schema.json')))+'\n\n以下是输入资料：\n'+encode(incoming)
    config=load_config(PROJECT)
    if config.model!='deepseek-flash':raise ValueError('配置模型已变化，不自动切换')
    reserve(root,phase,text)
    write(root/(phase+'-input.json'),incoming)
    (root/(phase+'-request.txt')).write_text(text,encoding='utf-8')
    started=time.monotonic()
    try:response=create_client(config,model='deepseek-flash').complete(text,max_tokens=PHASES[phase],require_complete=False,json_mode=True,timeout_seconds=config.analysis_timeout_seconds)
    except AIError as error:
        write(root/(phase+'-error.json'),{'time':now(),'error':str(error),'usage':'未知，不重试'});print(phase+': '+str(error));return
    write(root/(phase+'-response.json'),{'received_at':now(),'elapsed_seconds':round(time.monotonic()-started,2),**response})
    value,issues=processing.decode(response['text'],response['finish_reason'])
    if value is None:
        write(root/(phase+'-parse-issues.json'),issues);print(encode({'phase':phase,'usage':response['usage'],'parse_issues':issues}));return
    write(root/(phase+'-parsed.json'),value)
    result=processing.extraction(value,incoming['reviews']) if extracting else processing.report(value,incoming)
    write(root/(phase+'-processed.json'),result)
    print(encode({'phase':phase,'usage':response['usage'],'structure_issues':len(result['structure_issues']),
        'local_issues':len(result['issues']),'adjustments':len(result.get('adjustments',[])),
        'usable_reviews':len(result.get('reviews',[])),'findings':len(result.get('compiled',{}).get('findings',[]))}))


def finish(root):
    """汇总本轮调用和两库只读核对；参数：目录；返回：摘要。"""
    before=load(root/'database-before.json');after=snapshot();write(root/'database-after.json',after)
    changes=[{'database':d,'table':t} for d in before for t in before[d]['tables'] if before[d]['tables'][t]!=after[d]['tables'][t]]
    calls=[]
    for phase in PHASES:
        p=root/(phase+'-response.json')
        if p.exists():
            response=load(p);calls.append({k:response[k] for k in ('response_id','usage','finish_reason','returned_model')}|{'phase':phase})
    result={'trial_id':root.name,'calls':calls,'call_intents':len(list(root.glob('*-intent.json'))),
            'known_tokens':sum(x['usage'].get('total_tokens',0) for x in calls),'database_changes':changes,
            'versions_unchanged':all(before[d]['revision']==after[d]['revision'] for d in before)}
    write(root/'summary.json',result);print(encode(result))
    if changes or not result['versions_unchanged']:raise RuntimeError('前后数据变化，需核查并记录，不能宣称未修改')


def main():
    """解析独立试验命令；参数：命令行；返回：无。"""
    parser=argparse.ArgumentParser(description='独立V1.2试验；正式业务只读；联网必须显式allow-paid')
    parser.add_argument('action',choices=('export','prepare','offline','call','finish'))
    parser.add_argument('--root',type=Path);parser.add_argument('--baseline',type=Path)
    parser.add_argument('--phase',choices=PHASES);parser.add_argument('--allow-paid',action='store_true')
    args=parser.parse_args()
    if args.action=='export':export()
    elif args.action=='prepare':prepare(args.baseline)
    elif args.action=='offline':
        if not offline(args.root)['passed']:raise SystemExit(1)
    elif args.action=='call':call(args.root,args.phase,args.allow_paid)
    elif args.action=='finish':finish(args.root)


if __name__=='__main__':main()
