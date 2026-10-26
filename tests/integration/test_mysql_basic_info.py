"""验证库的基本信息写入、归属约束及历史转换；每例事务回滚。"""
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
import sqlalchemy as sa

from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.storage.collection_store import CollectionStore,CollectionWriter
from ecommerce.infrastructure.storage.mysql_migrations.basic_info_v7 import split_legacy_info
from ecommerce.infrastructure.tasks.ownership import atomic_json
from ecommerce.modules.collection.database_records import build_snapshot


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','显式启用验证库测试')
class MySQLBasicInfoTests(unittest.TestCase):
    def setUp(self):
        """连接独立验证库并建立回滚边界；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=replace(load_settings(root=Path(temp.name)),task_backend='mysql',collection_backend='mysql')
        self.assertTrue(self.settings.mysql.database.endswith('_verify'))
        self.store=CollectionStore(self.settings);self.store.tasks.initialize()
        self.c=self.store.engine.connect();self.addCleanup(self.c.close)
        tx=self.c.begin();self.addCleanup(tx.rollback)
        self.tables=self.store.tables;self.task=uuid4().hex;self.time=datetime(2026,10,25,12,34,56)
        self.c.execute(self.tables['tasks'].insert().values(id=self.task,name='example.basic_info',origin='live',status='succeeded',
                        started_at=self.time,finished_at=self.time))

    def source(self,status='complete',title='真实样例标题'):
        """保存临时基本信息原件与规范快照；参数：状态和标题；返回：引用。"""
        record={'schema_version':1,'product_id':'123','task_id':self.task,'batch_id':self.task+'-basic-'+uuid4().hex[:8],
                'scope':'product_basic_info','source_url':'https://item.taobao.com/item.htm?id=123',
                'captured_at':'2026-10-25T12:34:56+08:00','status':status,'product_info':{'title':title,'sales_text':'已售 1000+',
                'price_text':'¥3.9 起','captured_at':'2026-10-25T12:34:56+08:00','field_states':{'title':'provided'}}}
        path=self.settings.workspace/'history'/record['batch_id']/'basic_info.json';atomic_json(path,record)
        return build_snapshot(self.settings,'batch',record,path,module='basic_info')

    def test_write_and_replay_independent_basic_observation(self):
        reference=self.source();writer=CollectionWriter(self.settings,self.c)
        first=writer.apply(reference,{'sections':['product_info'],'last_attempt':True})
        writer.apply(reference,{'sections':['product_info'],'last_attempt':True})
        rows=self.c.execute(sa.select(self.tables['product_info_observations'])).mappings().all()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['price_text'],'¥3.9 起')
        self.assertEqual(rows[0]['captured_at'],self.time)
        self.assertEqual(first['counts']['product_info_observations'],1)
        self.assertEqual(self.c.scalar(sa.select(sa.func.count()).select_from(self.tables['sku_observations'])),0)

    def test_cross_product_and_module_ownership_rejected_by_database(self):
        result=CollectionWriter(self.settings,self.c).apply(self.source(),{'sections':['product_info']})
        info=self.c.execute(sa.select(self.tables['product_info_observations'])).mappings().one()
        product=self.c.execute(self.tables['products'].insert().values(platform='taobao',platform_product_id='456')).inserted_primary_key[0]
        for values in ({'product_id':product},{'module':'skus'}):
            with self.c.begin_nested(),self.assertRaises(sa.exc.DBAPIError) as rejected:
                self.c.execute(self.tables['product_info_observations'].update().where(self.tables['product_info_observations'].c.id==info['id']).values(**values))
            self.assertIn(rejected.exception.orig.args[0],{1452,3819})

    def test_partial_preserves_effective_pointer(self):
        first=CollectionWriter(self.settings,self.c).apply(self.source(),{'sections':['product_info'],'last_attempt':True})
        CollectionWriter(self.settings,self.c).apply(self.source('partial',None),{'sections':[],'last_attempt':True})
        pointer=self.c.execute(sa.select(self.tables['product_current_sections'])).mappings().one()
        self.assertEqual(pointer['batch_id'],first['id'])

    def legacy(self,status='partial',info=None):
        """创建实际旧结构样例；参数：SKU状态及原信息；返回：旧批次编号。"""
        product=self.c.execute(self.tables['products'].insert().values(platform='taobao',platform_product_id='789')).inserted_primary_key[0]
        payload={'record':{'schema_version':2,'product_info':info if info is not None else {'title':'旧标题','sales_text':'已售 2000+',
                    'captured_at':'2026-10-24T12:00:01+08:00'}}}
        old=self.c.execute(self.tables['collection_batches'].insert().values(platform='taobao',source_batch_key=uuid4().hex,
                    product_id=product,task_id=self.task,module='skus',status=status,scope='product_info_and_skus',scope_key='f'*64,
                    visibility='published',coverage_status='incomplete',captured_at=self.time,source_url='https://item.taobao.com/item.htm?id=789',
                    schema_version=2,result_payload=payload)).inserted_primary_key[0]
        return old

    def test_partial_sku_basic_information_is_migrated_independently_and_idempotently(self):
        old=self.legacy();before=dict(self.c.execute(sa.select(self.tables['collection_batches']).where(self.tables['collection_batches'].c.id==old)).mappings().one())
        split=split_legacy_info(self.c,self.tables)
        self.assertEqual(split['created'],1);self.assertEqual(split_legacy_info(self.c,self.tables)['created'],0)
        old_after=dict(self.c.execute(sa.select(self.tables['collection_batches']).where(self.tables['collection_batches'].c.id==old)).mappings().one())
        self.assertEqual(before,old_after)
        new=self.c.execute(sa.select(self.tables['collection_batches']).where(self.tables['collection_batches'].c.module=='basic_info')).mappings().one()
        self.assertEqual(new['status'],'complete');self.assertEqual(new['task_id'],self.task)
        self.assertEqual(new['derived_from_batch_id'],old);self.assertIsNone(new['step_id'])
        self.assertEqual(new['captured_at'],datetime(2026,10,24,12,0,1))

    def test_missing_old_info_is_not_invented(self):
        self.legacy(info={})
        self.assertEqual(split_legacy_info(self.c,self.tables)['created'],0)

    def test_unconfirmed_old_title_is_retained_without_valid_pointer(self):
        self.legacy(info={'sales_text':'已售 10+','captured_at':'2026-10-24T12:00:01+08:00'})
        split_legacy_info(self.c,self.tables)
        self.assertEqual(self.c.scalar(sa.select(self.tables['collection_batches'].c.status).where(self.tables['collection_batches'].c.module=='basic_info')),'partial')
        self.assertEqual(self.c.scalar(sa.select(sa.func.count()).select_from(self.tables['product_current_sections'])),0)


if __name__=='__main__':unittest.main()
