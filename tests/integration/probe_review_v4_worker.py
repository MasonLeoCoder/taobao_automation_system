"""验证库独立进程探针；显式模拟AI，精确清理本次合成记录，禁止正式库。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock,patch
from uuid import uuid4
import sqlalchemy as sa
from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.storage.mysql import create_mysql_engine
from ecommerce.infrastructure.storage.mysql_migrations.schema_v15 import build_metadata
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.modules.review_analysis import service,worker
from tests.unit.test_review_analysis_v4 import extraction,narrative


def write(path,value):
    """写本地验证记录，不含凭据；参数：路径、值；返回：无。"""
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


def child(settings,task_id,output):
    """仅执行指定合成任务并封锁真实客户端；参数：配置、任务、输出；返回：退出码。"""
    data=repository.read(settings,task_id)
    if any('脱敏工作进程测试' not in s['snapshot']['text'] for s in data['sources']):raise ValueError('非本探针的合成来源，拒绝执行')
    client=Mock()
    def complete(text,**options):
        """响应已固定的合成来源；参数：提示及选项；返回：模拟响应。"""
        incoming=json.loads(text.split('以下是输入资料：\n',1)[1])
        value=extraction(incoming) if 'reviews' in incoming else narrative(incoming)
        return {'text':json.dumps(value,ensure_ascii=False),'finish_reason':'stop','usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
    client.complete.side_effect=complete
    with patch.object(service,'create_client',return_value=client),patch('ecommerce.infrastructure.ai.client.DeepSeekClient.complete',side_effect=AssertionError('禁止真实AI')):
        code=worker.run(settings,task_id)
    result=repository.read(settings,task_id)
    write(output,{'task_id':task_id,'worker_exit':code,'execution_task_id':result['task']['execution_task_id'],
        'status':result['task']['status'],'processed_count':result['task']['processed_count'],'calls':len(result['calls']),
        'report_count':len(result['reports']),'mock_complete_calls':client.complete.call_count,'real_ai_calls':0})
    return code


def parent(settings,config,output):
    """临时提交合成任务供子进程读取，结束后精确清理；参数：配置、文件、输出；返回：无。"""
    tables=build_metadata().tables;engine=create_mysql_engine(settings.mysql)
    before={};task_id=None;execution=None;p=b=None;product=str(uuid4().int)[:18]
    try:
        with engine.begin() as conn:
            before={n:conn.scalar(sa.select(sa.func.count()).select_from(t)) for n,t in tables.items()}
            p=conn.execute(tables['products'].insert().values(platform='taobao',platform_product_id=product)).inserted_primary_key[0]
            b=conn.execute(tables['collection_batches'].insert().values(platform='taobao',source_batch_key=uuid4().hex,product_id=p,module='reviews',status='complete',scope_key='0'*64,coverage_status='limit_reached',visibility='published',captured_at=database_now())).inserted_primary_key[0]
            conn.execute(tables['review_observations'].insert().values(batch_id=b,position=1,review_key=uuid4().hex,identity_method='fingerprint_occurrence',identity_exact=False,content='脱敏工作进程测试初评',append_reviews=[],sku_text='合成规格'))
        preview=service.preview_v4(settings,product,b)
        with patch.object(service,'preview',service.preview_v4),patch.object(service,'launch'),patch.object(service,'create_client',return_value=Mock()):
            task_id=service.start(settings,product,batch_id=b,model=preview['model'],input_hash=preview['input_hash'],plan_hash=preview['plan_hash'],allow_paid=True)['id']
        child_output=output.with_name('worker-child.json')
        completed=subprocess.run([sys.executable,'-m',__spec__.name,'--config',str(config),'--task',task_id,'--output',str(child_output),'--child'],
            cwd=settings.root,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        output.with_suffix('.log').write_text(completed.stdout+completed.stderr,encoding='utf-8')
        result=json.loads(child_output.read_text(encoding='utf-8')) if child_output.exists() else {'worker_exit':completed.returncode}
        execution=repository.read(settings,task_id)['task']['execution_task_id']
        assert completed.returncode==0 and result['status']=='succeeded' and result['calls']==2 and result['mock_complete_calls']==2,result
        with engine.connect() as conn:
            events=conn.scalar(sa.select(sa.func.count()).select_from(tables['task_events']).where(tables['task_events'].c.task_id==execution))
        result.update(database=settings.mysql.database,synthetic=True,product_id=p,batch_id=b,event_count=events,cleaned=False)
    finally:
        # 只清理本探针已记录的主键，外键依赖顺序明确；绝不按整表删除。
        with engine.begin() as conn:
            if task_id:
                if execution is None:execution=conn.scalar(sa.select(tables['analysis_tasks'].c.execution_task_id).where(tables['analysis_tasks'].c.id==task_id))
                for name in ('analysis_review_decisions','analysis_reports','analysis_details','analysis_calls','analysis_sources'):
                    conn.execute(tables[name].delete().where(tables[name].c.analysis_id==task_id))
                conn.execute(tables['analysis_tasks'].delete().where(tables['analysis_tasks'].c.id==task_id))
            if execution:
                conn.execute(tables['task_events'].delete().where(tables['task_events'].c.task_id==execution))
                conn.execute(tables['tasks'].delete().where(tables['tasks'].c.id==execution))
            if b:
                conn.execute(tables['review_observations'].delete().where(tables['review_observations'].c.batch_id==b))
                conn.execute(tables['collection_batches'].delete().where(tables['collection_batches'].c.id==b))
            if p:conn.execute(tables['products'].delete().where(tables['products'].c.id==p))
        engine.dispose()
    with repository.connection(settings) as (conn,current):
        after={n:conn.scalar(sa.select(sa.func.count()).select_from(t)) for n,t in current.items()}
    assert before==after
    result.update(cleaned=True,before=before,after=after);write(output,result)
    print(json.dumps({k:result[k] for k in ('task_id','execution_task_id','status','calls','event_count','real_ai_calls','cleaned')},ensure_ascii=True))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--task');parser.add_argument('--child',action='store_true');args=parser.parse_args()
    settings=load_settings(str(args.config),root=Path.cwd())
    if settings.mysql.database!='taobao_automation_verify':raise ValueError('仅允许明确的独立验证库')
    if args.child:raise SystemExit(child(settings,args.task,args.output))
    parent(settings,args.config.resolve(),args.output.resolve())
