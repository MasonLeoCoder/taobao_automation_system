"""本地已有返回的派生与复核入口；没有AI请求入口，不覆盖任何旧报告。"""
import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from tests.prompt_trials_v1_2.runner import load,write,checksum,snapshot
from tests.report_closeout import core


def now():
    """返回北京时间；参数：无；返回：ISO时间。"""
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def verify_baseline(root):
    """核对冻结文件与助手意见绑定；参数：收尾目录；返回：来源目录及整体身份。"""
    hashes=load(root/'baseline-hashes.json')
    if not all(Path(p).exists() and checksum(p)==h for p,h in hashes.items()):raise ValueError('冻结来源发生变化，停止派生')
    binding=core.digest(hashes);assessment=load(root/'assistant-assessment.json')
    if assessment['baseline_digest']!=binding or assessment['author_type']!='assistant':raise ValueError('助手意见未绑定当前来源')
    return Path(load(root/'manifest.json')['baseline']),binding


def reports(root):
    """从已有提取及派生生成两份报告；参数：目录；返回：报告字典，原文件不改。"""
    baseline,binding=verify_baseline(root);assessment=load(root/'assistant-assessment.json')
    real=load(baseline/'real_extract-processed.json')['reviews']
    result={}
    for phase in ('complete_report','partial_report'):
        incoming=load(baseline/(phase+'-input.json'))
        included={r['id'] for r in incoming['extraction']['reviews']}
        rows=[r for r in real if r['id'] in included]
        result[phase]=core.build(phase,incoming,rows,load(baseline/(phase+'-processed.json')),assessment['phases'][phase],binding)
    return result


def read_receipts(root):
    """读取追加式人工记录；参数：目录；返回：顺序数组。"""
    directory=root/'decisions'
    return [load(p) for p in sorted(directory.glob('*.json'))] if directory.exists() else []


def publish(root):
    """另建派生修订并渲染，不覆盖历史；参数：目录；返回：新修订目录。"""
    from tests.report_closeout.render import render
    result=reports(root);receipts=read_receipts(root)
    revisions=root/'revisions';revisions.mkdir(exist_ok=True)
    count=max([int(p.name) for p in revisions.iterdir() if p.is_dir() and p.name.isdigit()],default=0)+1
    destination=revisions/f'{count:04d}';destination.mkdir()
    for phase,value in result.items():write(destination/(phase+'.json'),core.apply_decisions(value,receipts))
    baseline,_=verify_baseline(root)
    synthetic=load(baseline/'synthetic_extract-processed.json')['reviews']
    write(destination/'expression-views.json',{'real':core.source_views(load(baseline/'real_extract-processed.json')['reviews']),
        'synthetic':core.source_views(synthetic),'note':'合成样例独立展示，不进入两份真实报告统计；表达可追溯不等于性能独立验证。'})
    write(destination/'revision.json',{'version':'review-report-closeout-v1','created_at':now(),
        'assessment_sha256':checksum(root/'assistant-assessment.json'),'decision_files':{p.name:checksum(p) for p in sorted((root/'decisions').glob('*.json'))},
        'source_baseline_digest':core.digest(load(root/'baseline-hashes.json')),'new_ai_calls':0})
    render(root,destination)
    print(str(destination.resolve()));return destination


def decide(root,item_id,decision,reviewer,comment,user_confirmed=False):
    """保存明确人工决定并保留历史；参数：目录、条目、状态、填写人和意见及确认；返回：新记录。"""
    if not user_confirmed:raise ValueError('仅在用户明确填写审核决定时使用确认参数；助手不能代替用户确认')
    found=[(phase,item) for phase,r in reports(root).items() for item in r['review_items'] if item['id']==item_id]
    if len(found)!=1:raise ValueError('复核条目不存在或不唯一')
    phase,item=found[0]
    receipt={'phase':phase,'item_id':item_id,'decision':decision,'reviewer':reviewer,'comment':comment,
        'user_confirmed':True,'created_at':now(),'baseline_digest':verify_baseline(root)[1],'candidate_digest':item['candidate_digest']}
    # 与展示采用相同校验；参数是本地明确声明，不声称提供身份认证。
    core.apply_decisions(reports(root)[phase],[receipt])
    directory=root/'decisions';directory.mkdir(exist_ok=True)
    index=max([int(p.stem) for p in directory.glob('*.json')],default=0)+1
    with (directory/f'{index:06d}.json').open('x',encoding='utf-8') as stream:json.dump(receipt,stream,ensure_ascii=False,indent=2)
    return receipt


def finish(root):
    """只读核对两库与冻结来源；参数：目录；返回：无。"""
    verify_baseline(root)
    after=snapshot();before=load(root/'database-before.json');write(root/'database-after.json',after)
    if before!=after:raise ValueError('两库前后不一致，不能声明业务数据不变')
    write(root/'finish.json',{'finished_at':now(),'database_unchanged':True,'baseline_unchanged':True,
        'new_ai_calls':0,'added_tokens':0,'user_accepted':False})


def main():
    """解析离线命令；参数：命令行；返回：无。"""
    parser=argparse.ArgumentParser(description='已有评价报告离线收尾，不调用AI')
    parser.add_argument('action',choices=('publish','decide','finish'));parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--item');parser.add_argument('--decision',choices=core.DECISIONS)
    parser.add_argument('--reviewer');parser.add_argument('--comment');parser.add_argument('--user-confirmed',action='store_true')
    args=parser.parse_args();root=args.root.resolve()
    if args.action=='publish':publish(root)
    elif args.action=='finish':finish(root)
    else:decide(root,args.item,args.decision,args.reviewer,args.comment,args.user_confirmed)


if __name__=='__main__':main()
