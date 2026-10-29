"""使用本机单账号配置，在独立验证库核对后台凭据引用；不访问真实平台。"""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.shared.config import load_settings,settings_payload,mysql_password
from ecommerce.infrastructure.storage.collection_jobs import CollectionJobStore
from ecommerce.infrastructure.storage.database import now
from ecommerce.infrastructure.tasks.ownership import execution_key
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.infrastructure.tasks import web_jobs
from ecommerce.modules.collection import web_worker


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','显式启用独立MySQL凭据验证')
class MySQLCredentialIntegrationTests(unittest.TestCase):
    def setUp(self):
        """复用本机凭据并固定验证库；参数：无；返回：无。"""
        current=load_settings()
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings=replace(load_settings(root=Path(temporary.name)),task_backend='mysql',collection_backend='mysql',
            mysql=replace(current.mysql,database='taobao_automation_verify'))
        self.assertTrue(self.settings.mysql.database.endswith('_verify'))
        self.store=CollectionJobStore(self.settings)
        self.store.tasks.initialize()
        self.key=execution_key(self.settings.for_platform('taobao'))
        self.identifiers=[]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        """仅清理本例的申请、执行及控制记录；参数：无；返回：无。"""
        t=self.store.tables
        with self.store.engine.begin() as connection:
            connection.execute(t['collection_job_controls'].delete().where(t['collection_job_controls'].c.job_id.in_(self.identifiers)))
            connection.execute(t['collection_jobs'].delete().where(t['collection_jobs'].c.id.in_(self.identifiers)))
            connection.execute(t['task_events'].delete().where(t['task_events'].c.task_id.in_(self.identifiers)))
            connection.execute(t['tasks'].delete().where(t['tasks'].c.id.in_(self.identifiers)))
            connection.execute(t['collection_queue_states'].delete().where(t['collection_queue_states'].c.execution_key==self.key))

    def submit(self,*,legacy=False):
        """提交明确标记的合成申请；参数：是否旧账号快照；返回：申请编号。"""
        identifier=uuid4().hex
        self.identifiers.append(identifier)
        config=settings_payload(self.settings)
        if legacy:
            config['mysql'].pop('credentials_file',None)
            config['mysql'].update(user='taobao_app',migration_user='taobao_migrator',migration_password_env='LEGACY_UNUSED')
        self.store.submit({'id':identifier,'execution_key':self.key,'created_at':now(),'product_id':'123','module':'selected',
            'mode':'detail','modules':['video'],'skipped_modules':[],
            'options':{'mode':'detail','input':'123','modules':['video'],'all_reviews':False,'all_questions':False},'config':config})
        return identifier

    def test_actual_worker_uses_current_credentials_and_database_has_no_password(self):
        """旧快照的申请由当前账号执行，持久配置不含实际密码，任务可真实收尾。"""
        identifier=self.submit(legacy=True)
        claimed=self.store.claim(self.key,identifier)
        self.assertIsNotNone(claimed)
        observed=[]
        def synthetic(settings,row):
            """只运行统一任务记录流程，不打开网页；参数：配置、申请；返回：真实执行结论。"""
            observed.append(settings.mysql.user)
            return run_task(settings.for_platform('taobao'),'collection.taobao.credential_validation',
                lambda:'独立库凭据引用合成验证',task_id=identifier,job_claim_token=claimed['claim_token'])
        with patch.object(web_worker,'validate_job'),patch.object(web_worker,'run_collection',side_effect=synthetic),\
             patch('ecommerce.modules.collection.dispatch.after_execution'):
            web_worker.execute(self.settings.workspace,identifier,claimed['claim_token'],bootstrap=self.settings)
        self.assertEqual(observed,[self.settings.mysql.user])
        job=self.store.read(identifier)
        self.assertEqual(job['task_status'],'succeeded')
        with self.store.engine.connect() as connection:
            payload=connection.scalar(sa.select(self.store.tables['collection_jobs'].c.payload).where(self.store.tables['collection_jobs'].c.id==identifier))
        self.assertFalse(mysql_password(self.settings.mysql) in json.dumps(payload,ensure_ascii=False),'任务记录不得包含实际数据库密码')

    def test_separate_child_connects_via_reference_without_opening_browser(self):
        """真实独立进程通过本地引用读验证库；无有效领取时退出，不执行采集。"""
        identifier=self.submit()
        process=web_jobs.launch(self.settings,identifier,'invalid-claim-token')
        self.addCleanup(lambda:process.kill() if process.poll() is None else None)
        self.assertEqual(process.wait(timeout=30),0)
        self.assertIsNone(self.store.read(identifier)['task_id'])
        text=(web_jobs.directory(self.settings)/(identifier+'.log')).read_text(encoding='utf-8')
        self.assertFalse(mysql_password(self.settings.mysql) in text,'子进程日志不得包含实际数据库密码')
