"""分析五表真实MySQL验证；独立验证库、模拟AI、所有合成记录回滚。"""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from uuid import uuid4
import sqlalchemy as sa
from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.storage.mysql import create_mysql_engine
from ecommerce.infrastructure.storage.mysql_migrations.schema_v13 import build_metadata, NEW_TABLES
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.modules.review_analysis import service


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS') == '1', '显式启用验证库')
class MySQLAnalysisTests(unittest.TestCase):
    def test_transaction_constraints_and_analysis_flow(self):
        """真实事务执行模拟分析和人工修订，唯一约束及来源外键生效，最后回滚。"""
        base = load_settings()
        settings = replace(base, mysql=replace(base.mysql, database='taobao_automation_verify'))
        self.assertTrue(settings.mysql.database.endswith('_verify'))
        engine = create_mysql_engine(settings.mysql); self.addCleanup(engine.dispose)
        tables = build_metadata().tables
        token = uuid4().hex
        product = str(uuid4().int)[:18]
        with engine.connect() as conn:
            transaction = conn.begin()
            class Proxy:
                """把业务短事务映射到验证用外层回滚事务。"""
                @contextmanager
                def begin(self):
                    """返回保存点；参数：无；返回：同连接。"""
                    with conn.begin_nested(): yield conn
                @contextmanager
                def connect(self):
                    """返回只读连接；参数：无；返回：同连接，不关闭外层。"""
                    yield conn
            try:
                before = {n:conn.scalar(sa.select(sa.func.count()).select_from(tables[n])) for n in NEW_TABLES}
                p = conn.execute(tables['products'].insert().values(platform='taobao', platform_product_id=product)).inserted_primary_key[0]
                b = conn.execute(tables['collection_batches'].insert().values(platform='taobao', source_batch_key=token,
                    product_id=p, module='reviews', status='complete', scope_key='0'*64, coverage_status='limit_reached',
                    visibility='published', captured_at=database_now())).inserted_primary_key[0]
                r = conn.execute(tables['review_observations'].insert().values(batch_id=b, position=1, review_key=token,
                    identity_method='fingerprint_occurrence', identity_exact=False, content='初评满意',
                    append_reviews=[{'content':'后来不粘了'}], sku_text='合成规格')).inserted_primary_key[0]
                store = SimpleNamespace(engine=Proxy(), tables=tables)
                with patch('ecommerce.infrastructure.storage.analysis_store.collection_store', return_value=store), patch('ecommerce.modules.review_analysis.service.launch'):
                    preview = service.preview(settings, product, b)
                    task = service.start(settings, product, batch_id=b, model=preview['model'], input_hash=preview['input_hash'],
                        max_calls=10, max_tokens=300000, allow_paid=True)['id']
                    initial = repository.read(settings, task)
                    self.assertEqual(len(initial['task']['parameters']['plan']['groups']), 1)
                    self.assertTrue(initial['task']['parameters']['plan']['json_mode'])
                    t = tables['analysis_tasks']; row = dict(initial['task'])
                    with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                        conn.execute(t.insert().values(**{**row, 'id':uuid4().hex}))
                    source = initial['sources'][0]
                    with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                        conn.execute(tables['analysis_sources'].insert().values(**{**source,'id':uuid4().hex,'review_id':2**63-1}))
                    with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                        conn.execute(tables['review_observations'].delete().where(tables['review_observations'].c.id==r))
                    client = Mock()
                    client.complete.side_effect = [
                        {'text':json.dumps({'reviews':[{'id':r,'kind':'specific','points':[{'topic':'粘性','sentiment':'negative','segment':'append:1','quote':'后来不粘了'}]}]},ensure_ascii=False),'finish_reason':'stop','usage':{'total_tokens':20}},
                        {'text':json.dumps({'findings':[{'title':'追评线索','text':'追评反映粘性问题，需核查。','evidence':[{'id':r,'segment':'append:1','quote':'后来不粘了'}]}],'suggestions':[]},ensure_ascii=False),'finish_reason':'stop','usage':{'total_tokens':10}}]
                    with patch('ecommerce.modules.review_analysis.service.create_client', return_value=client):
                        self.assertEqual(service.execute(settings,task,Mock()),'succeeded')
                    result = service.read(settings, task)
                    self.assertEqual(result['task']['usage']['total_tokens'],30)
                    self.assertEqual(result['budget']['used_tokens'], 30)
                    self.assertEqual(result['report']['content']['statistics']['topics'][0]['counts']['negative'],1)
                    detail = result['details'][0]
                    service.correct(settings,task,detail['id'],expected_revision=0,kind='specific',
                        points=[{**detail['result']['points'][0],'sentiment':'unclear'}],excluded=False,reason='合成复核原因')
                    corrected = repository.read(settings,task)
                    self.assertEqual(len(corrected['reports']),2)
                    self.assertEqual(corrected['details'][0]['revision'],1)
                    self.assertEqual(client.complete.call_count,2)
                    after = {n:conn.scalar(sa.select(sa.func.count()).select_from(tables[n])) for n in NEW_TABLES}
                    self.assertEqual({n:after[n]-before[n] for n in NEW_TABLES},dict(zip(NEW_TABLES,[1,1,2,1,2])))
                    second = service.start(settings, product, batch_id=b, model=preview['model'], input_hash=preview['input_hash'],
                        allow_paid=True, force=True)['id']
                    params = dict(repository.read(settings, second)['task']['parameters'])
                    conn.execute(t.update().where(t.c.id == second).values(parameters={**params,'max_calls':1}))
                    partial_client = Mock()
                    partial_client.complete.return_value = result['calls'][0]['response']
                    with patch('ecommerce.modules.review_analysis.service.create_client', return_value=partial_client):
                        self.assertEqual(service.execute(settings, second, Mock()), 'partial')
                    with self.assertRaisesRegex(ValueError, '额度不足'):
                        service.resume(settings, second, allow_paid=True)
                    service.resume(settings, second, allow_paid=True, max_calls=2)
                    report_client = Mock()
                    report_client.complete.return_value = result['calls'][1]['response']
                    with patch('ecommerce.modules.review_analysis.service.create_client', return_value=report_client):
                        self.assertEqual(service.execute(settings, second, Mock()), 'succeeded')
                    continued = repository.read(settings, second)
                    self.assertEqual(len(continued['details']), 1)
                    self.assertEqual(len(continued['calls']), 2)
                    self.assertEqual(continued['task']['parameters']['budget_history'][0]['after']['max_calls'], 2)
                    self.assertEqual(report_client.complete.call_count, 1)
                    other=conn.execute(tables['review_observations'].insert().values(batch_id=b,position=2,review_key=token+'recover',
                        identity_method='fingerprint_occurrence',identity_exact=False,content='满意',append_reviews=[],sku_text='合成规格')).inserted_primary_key[0]
                    current=service.preview(settings,product,b)
                    recovered=service.start(settings,product,batch_id=b,model=current['model'],input_hash=current['input_hash'],
                        allow_paid=True,force=True)['id']
                    bad_response={'text':json.dumps({'reviews':[{'id':r,'kind':'specific','points':[{
                        'topic':'粘性','sentiment':'negative','segment':'append:1','quote':'不存在的拼接引文'}]},
                        {'id':other,'kind':'general','points':[]}]},ensure_ascii=False),'finish_reason':'stop','usage':{'total_tokens':20}}
                    bad_call=repository.begin_call(settings,recovered,'classify',1,'模拟原始请求',[r,other],1000)
                    repository.finish_call(settings,recovered,bad_call,'invalid',bad_response,'模拟旧版整组拒收')
                    repository.update_task(settings,recovered,status='failed',active_key=None,owner=None)
                    with patch('ecommerce.modules.review_analysis.service.create_client') as no_ai:
                        restored=service.recover_saved(settings,recovered)
                        self.assertEqual(restored['processed_count'],1)
                        self.assertEqual(service.recover_saved(settings,recovered)['added_count'],0)
                        no_ai.assert_not_called()
                    self.assertEqual(repository.read(settings,recovered)['calls'][0]['response'],bad_response)
                    remaining=service.read(settings,recovered)['resume_budget']
                    self.assertEqual(remaining['supplement']['source_ids'],[r])
                    service.resume(settings,recovered,allow_paid=True,max_calls=3,plan_hash=remaining['plan_hash'])
                    supplemental=Mock();supplemental.complete.side_effect=[result['calls'][0]['response'],result['calls'][1]['response']]
                    with patch('ecommerce.modules.review_analysis.service.create_client',return_value=supplemental):
                        self.assertEqual(service.execute(settings,recovered,Mock()),'succeeded')
                    recovered_data=repository.read(settings,recovered)
                    self.assertEqual(len(recovered_data['details']),2)
                    self.assertEqual(recovered_data['calls'][1]['request']['source_ids'],[r])
                    self.assertEqual(recovered_data['calls'][0]['status'],'invalid')
                    self.assertEqual(recovered_data['calls'][0]['response'],bad_response)
                    self.assertEqual(supplemental.complete.call_count,2)
                    print('Verify-only analysis flow passed; synthetic task '+task+' rolled back; AI mocked.')
            finally:
                transaction.rollback()
            counts = {n:conn.scalar(sa.select(sa.func.count()).select_from(tables[n])) for n in NEW_TABLES}
            self.assertEqual(counts,before)
