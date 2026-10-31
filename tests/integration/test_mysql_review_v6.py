"""新直接报告在独立MySQL的真实事务验证，模拟接口，外层事务全部回滚。"""
from contextlib import contextmanager
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase,skipUnless
from unittest.mock import Mock,patch
from uuid import uuid4
import sqlalchemy as sa
from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.storage.mysql import create_mysql_engine
from ecommerce.infrastructure.storage.mysql_migrations.schema_v18 import build_metadata
from ecommerce.infrastructure.storage.time_values import database_now
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.modules.review_analysis import service
from tests.unit.test_review_analysis_v6 import report_for


@skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','需显式启用独立验证库')
class MySQLReviewV6Tests(TestCase):
    def test_direct_messages_report_cache_and_full_rollback(self):
        """核对新消息持久化、成品缓存及零伪分类；参数：环境配置；返回：全回滚。"""
        verified=load_settings(os.environ['ECOMMERCE_MYSQL_CONFIG'],root=Path.cwd());self.assertEqual(verified.mysql.database,'taobao_automation_verify')
        engine=create_mysql_engine(verified.mysql);self.addCleanup(engine.dispose);tables=build_metadata().tables
        with TemporaryDirectory() as folder,engine.connect() as conn:
            transaction=conn.begin();root=Path(folder);(root/'config').mkdir();(root/'config/ai.toml').write_text('[ai]\napi_key="sk-test-only"',encoding='utf-8')
            settings=replace(load_settings(root=root),mysql=verified.mysql,collection_backend='mysql',task_backend='mysql')
            class Proxy:
                """用真实保存点承接业务事务。"""
                @contextmanager
                def begin(self):
                    """返回保存点事务；参数：无；返回：连接。"""
                    with conn.begin_nested():yield conn
                @contextmanager
                def connect(self):
                    """读取外层事务；参数：无；返回：连接。"""
                    yield conn
            before={n:conn.scalar(sa.select(sa.func.count()).select_from(t)) for n,t in tables.items()}
            try:
                token=uuid4().hex;product=str(uuid4().int)[:18]
                p=conn.execute(tables['products'].insert().values(platform='taobao',platform_product_id=product)).inserted_primary_key[0]
                b=conn.execute(tables['collection_batches'].insert().values(platform='taobao',source_batch_key=token,product_id=p,module='reviews',status='complete',scope_key='0'*64,coverage_status='limit_reached',visibility='published',captured_at=database_now())).inserted_primary_key[0]
                review=conn.execute(tables['review_observations'].insert().values(batch_id=b,position=1,review_key=token,identity_method='fingerprint_occurrence',identity_exact=False,content='脱敏测试初评',append_reviews=[{'content':'脱敏追评反馈'}],sku_text='合成规格')).inserted_primary_key[0]
                client=Mock()
                def complete(messages,**options):
                    """模拟实际两条消息；参数：消息、选项；返回：报告和用量。"""
                    text=report_for(json.loads(messages[-1]['content'])).replace('【评价','【')+f'\n\n【{review}｜初评】原文：“脱敏……初评”'
                    return {'text':text,'finish_reason':'stop','usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
                client.complete_messages.side_effect=complete
                with patch.object(repository,'collection_store',return_value=SimpleNamespace(engine=Proxy(),tables=tables)),patch.object(service,'launch') as launch,patch.object(service,'create_client',return_value=client):
                    preview=service.preview(settings,product,b);self.assertEqual(preview['planned_calls'],1)
                    task=service.start(settings,product,batch_id=b,model=preview['model'],input_hash=preview['input_hash'],plan_hash=preview['plan_hash'],allow_paid=True,max_calls=1)['id']
                    self.assertEqual(service.execute(settings,task,Mock()),'succeeded');data=service.read(settings,task)
                    self.assertEqual(data['task']['processed_count'],0);self.assertEqual(data['details'],[]);self.assertEqual(data['task']['usage']['total_tokens'],20)
                    self.assertEqual(len(data['calls'][0]['request']['messages']),2);self.assertFalse(data['calls'][0]['request']['json_mode'])
                    self.assertEqual(data['v6_content']['validation']['state'],'needs_review');self.assertEqual(data['v6_content']['coverage']['submitted_count'],1)
                    self.assertTrue(data['v6_content']['generation']['complete']);self.assertEqual(data['v6_content']['validation']['counts']['pending_count'],1)
                    reused=service.start(settings,product,batch_id=b,model=preview['model'],input_hash=preview['input_hash'],allow_paid=True,max_tokens=preview['default_tokens']+1000)
                    self.assertEqual(reused['id'],task);self.assertTrue(reused['choice_required']);launch.assert_called_once()
                    self.assertEqual(client.complete_messages.call_count,1)
                    old=copy.deepcopy(data['report']['content']);old.pop('generation');old.pop('inspection')
                    old['validation']={'state':'needs_review','complete':False,'citations':[],'issues':['脱敏旧规则未识别。']};old['coverage']['referenced_count']=0
                    conn.execute(tables['analysis_reports'].update().where(tables['analysis_reports'].c.id==data['report']['id']).values(content=old))
                    conn.execute(tables['analysis_tasks'].update().where(tables['analysis_tasks'].c.id==task).values(status='partial',message='脱敏旧规则未识别。'))
                    conn.execute(tables['analysis_calls'].update().where(tables['analysis_calls'].c.analysis_id==task).values(status='invalid'))
                    before_check=repository.read(settings,task)
                    service.recount(settings,task,report_id=data['report']['id']);checked=repository.read(settings,task)
                    self.assertEqual(len(checked['reports']),2);self.assertEqual(checked['task']['status'],'succeeded')
                    self.assertEqual(checked['calls'],before_check['calls']);self.assertEqual(checked['reports'][0],before_check['reports'][0]);self.assertEqual(checked['sources'],before_check['sources'])
                    self.assertEqual(checked['report']['content']['inspection']['origin'],'local_recheck');self.assertEqual(checked['report']['content']['validation']['counts']['valid_count'],1)
            finally:transaction.rollback()
            after={n:conn.scalar(sa.select(sa.func.count()).select_from(t)) for n,t in tables.items()};self.assertEqual(before,after)
            if os.environ.get('ECOMMERCE_MYSQL_REPORT'):
                Path(os.environ['ECOMMERCE_MYSQL_REPORT']).write_text(json.dumps({'passed':True,'synthetic':True,'rolled_back':True,'task_id':task,'product_id':p,'platform_product_id':product,'batch_id':b,'review_id':review,'report_id':checked['report']['id'],'mock_calls':1,'real_ai_calls':0,'local_checks':checked['report']['content']['validation']['counts'],'before':before,'after':after},indent=2),encoding='utf-8')
