"""独立验证库新版分析真实事务；模拟AI、外层事务全部回滚。"""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4
import sqlalchemy as sa
from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.storage.mysql import create_mysql_engine
from ecommerce.infrastructure.storage.mysql_migrations.schema_v14 import build_metadata
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.storage import analysis_store as repository,analysis_review_store
from ecommerce.modules.review_analysis import service
from tests.unit.test_review_analysis_v4 import extraction,narrative


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','需明确启用独立验证库')
class MySQLReviewV4Tests(unittest.TestCase):
    def test_mysql_pipeline_review_foreign_keys_and_full_rollback(self):
        """验证新版持久化、复核并发及复合归属，所有合成业务回滚。"""
        config=os.environ.get('ECOMMERCE_MYSQL_CONFIG')
        if not config:self.skipTest('需指定workspace内的独立验证配置')
        verified=load_settings(config,root=Path.cwd())
        self.assertEqual(verified.mysql.database,'taobao_automation_verify')
        engine=create_mysql_engine(verified.mysql);self.addCleanup(engine.dispose);tables=build_metadata().tables
        with TemporaryDirectory() as folder,engine.connect() as conn:
            transaction=conn.begin();root=Path(folder);(root/'config').mkdir()
            (root/'config/ai.toml').write_text('[ai]\napi_key="sk-test-only"',encoding='utf-8')
            settings=replace(load_settings(root=root),mysql=verified.mysql,collection_backend='mysql',task_backend='mysql')
            class Proxy:
                """业务事务映射到回滚事务内保存点。"""
                @contextmanager
                def begin(self):
                    """返回同连接保存点；参数：无；返回：连接。"""
                    with conn.begin_nested():yield conn
                @contextmanager
                def connect(self):
                    """读取当前外层事务；参数：无；返回：连接。"""
                    yield conn
            before={n:conn.scalar(sa.select(sa.func.count()).select_from(t)) for n,t in tables.items()}
            try:
                token=uuid4().hex;product=str(uuid4().int)[:18]
                p=conn.execute(tables['products'].insert().values(platform='taobao',platform_product_id=product)).inserted_primary_key[0]
                b=conn.execute(tables['collection_batches'].insert().values(platform='taobao',source_batch_key=token,product_id=p,module='reviews',status='complete',scope_key='0'*64,coverage_status='limit_reached',visibility='published',captured_at=database_now())).inserted_primary_key[0]
                review_id=conn.execute(tables['review_observations'].insert().values(batch_id=b,position=1,review_key=token,identity_method='fingerprint_occurrence',identity_exact=False,content='脱敏测试初评',append_reviews=[{'content':'测试追评反馈'}],sku_text='合成规格')).inserted_primary_key[0]
                client=Mock()
                def complete(text,**options):
                    incoming=json.loads(text.split('以下是输入资料：\n',1)[1]);value=extraction(incoming) if 'reviews' in incoming else narrative(incoming)
                    return {'text':json.dumps(value,ensure_ascii=False),'finish_reason':'stop','usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
                client.complete.side_effect=complete
                with patch.object(repository,'collection_store',return_value=SimpleNamespace(engine=Proxy(),tables=tables)),patch.object(service,'launch'),patch.object(service,'create_client',return_value=client):
                    preview=service.preview(settings,product,b)
                    task=service.start(settings,product,batch_id=b,model=preview['model'],input_hash=preview['input_hash'],plan_hash=preview['plan_hash'],allow_paid=True)['id']
                    self.assertEqual(service.execute(settings,task,Mock()),'succeeded')
                    result=service.read(settings,task);self.assertEqual(result['task']['usage']['total_tokens'],40)
                    item=result['v4_content']['review_items'][0]
                    values={'report_id':result['report']['id'],'item_key':item['key'],'candidate_hash':item['candidate_hash'],'source_hash':result['v4_content']['source_hash'],
                        'expected_revision':0,'decision':'adopted','reviewer':'合成测试人','reason':'测试核对','review_context':{'evidence_checked':True}}
                    analysis_review_store.save(settings,task,**values)
                    with self.assertRaises(ValueError):analysis_review_store.save(settings,task,**values)
                    record=dict(conn.execute(sa.select(tables['analysis_review_decisions'])).mappings().one())
                    with self.assertRaises(sa.exc.IntegrityError),conn.begin_nested():conn.execute(tables['analysis_review_decisions'].insert().values(**{**record,'id':uuid4().hex}))
                    other=service.start(settings,product,batch_id=b,model=preview['model'],input_hash=preview['input_hash'],allow_paid=True,force=True)['id']
                    with self.assertRaises(sa.exc.IntegrityError),conn.begin_nested():conn.execute(tables['analysis_review_decisions'].insert().values(**{**record,'id':uuid4().hex,'analysis_id':other,'revision':2}))
                    call=result['calls'][-1]
                    with conn.begin_nested():
                        conn.execute(tables['analysis_calls'].insert().values(**{**call,'id':uuid4().hex,'chunk_index':1}))
                    with self.assertRaises(sa.exc.DBAPIError) as invalid,conn.begin_nested():conn.execute(tables['analysis_calls'].insert().values(**{**call,'id':uuid4().hex,'chunk_index':-1}))
                    self.assertEqual(invalid.exception.orig.args[0],3819)
                    service.recount(settings,task)
                    with self.assertRaises(ValueError):analysis_review_store.save(settings,task,**{**values,'expected_revision':1})
                    self.assertEqual(client.complete.call_count,2)
            finally:transaction.rollback()
            after={n:conn.scalar(sa.select(sa.func.count()).select_from(t)) for n,t in tables.items()}
            self.assertEqual(before,after)
            if os.environ.get('ECOMMERCE_MYSQL_REPORT'):
                from tests.prompt_trials_v1_2.runner import write
                write(Path(os.environ['ECOMMERCE_MYSQL_REPORT']),{'passed':True,'database':verified.mysql.database,'synthetic':True,
                    'task_id':task,'other_task_id':other,'product_id':p,'platform_product_id':product,'batch_id':b,'review_id':review_id,
                    'report_id':result['report']['id'],'decision_id':record['id'],'calls':client.complete.call_count,'ai_mocked':True,'rolled_back':True,'before':before,'after':after})
