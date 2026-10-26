"""验证库销量写入、旧快照重放及数据库约束；每例事务回滚。"""
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
from ecommerce.shared.sales import FIELDS, parse_sales
from ecommerce.infrastructure.storage.collection_store import CollectionStore, CollectionWriter, CollectionDataError
from ecommerce.infrastructure.storage.collection_snapshot import open_snapshot, save_snapshot
from ecommerce.modules.collection.database_records import build_snapshot


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','显式启用验证库销量测试')
class MySQLSalesTests(unittest.TestCase):
    def setUp(self):
        """准备临时来源及回滚事务；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings=replace(load_settings(root=Path(temp.name)),task_backend='mysql',collection_backend='mysql')
        self.assertEqual(self.settings.mysql.database,'taobao_automation_verify')
        self.store=CollectionStore(self.settings)
        self.store.tasks.initialize()
        self.connection=self.store.engine.connect()
        self.addCleanup(self.connection.close)
        transaction=self.connection.begin()
        self.addCleanup(transaction.rollback)
        self.task=uuid4().hex
        self.connection.execute(self.store.tables['tasks'].insert().values(id=self.task,name='example.sales',origin='live',
            status='succeeded',started_at=datetime(2026,10,25,12),finished_at=datetime(2026,10,25,12,1)))
        self.table=self.store.tables['list_observations']

    def source(self, legacy=False):
        """构造列表快照；参数：是否删除新字段模拟旧快照；返回：固定引用。"""
        record={'schema_version':4 if legacy else 6,'task_id':self.task,'batch_id':self.task+'-list',
            'keyword':'销量样例','search_method':'keyword','page_number':1,'sort_label':'销量',
            'collection_status':'limit_reached','scope':'search_page_loaded_cards','requested_limit':1,
            'items':[{'product_id':'123456','product_index':1,'slot_index':1,'title':'样例','sales_text':'1.2万+人收货'}]}
        path=self.settings.workspace/'sources/sample.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
        reference=build_snapshot(self.settings,'batch',record,path,module='list')
        if not legacy: return reference
        header, iterator=open_snapshot(self.settings,reference)
        rows=list(iterator)
        for row in rows:
            if row['table']=='list_observations':
                for name in FIELDS: row['values'].pop(name,None)
        return save_snapshot(self.settings,header,rows)

    def test_new_snapshot_persists_five_fields(self):
        """新快照从原文保存数量下界，数据库原文及观察时间语义不变。"""
        CollectionWriter(self.settings,self.connection).apply(self.source())
        row=self.connection.execute(sa.select(self.table)).mappings().one()
        self.assertEqual({name:row[name] for name in FIELDS},parse_sales('1.2万+人收货'))

    def test_old_snapshot_replay_preserves_filled_fields_and_bootstraps_pending(self):
        """旧快照缺新字段可重放，已回填结果不清空；待解析可初始化且其他冲突仍拒绝。"""
        reference=self.source(legacy=True)
        writer=CollectionWriter(self.settings,self.connection)
        writer.apply(reference)
        first=dict(self.connection.execute(sa.select(self.table)).mappings().one())
        writer.apply(reference)
        self.assertEqual(dict(self.connection.execute(sa.select(self.table)).mappings().one()),first)
        self.connection.execute(self.table.update().where(self.table.c.id==first['id']).values(
            sales_count=None,sales_precision='unknown',sales_metric='unknown',sales_period='unknown',sales_parse_status='pending'))
        CollectionWriter(self.settings,self.connection).apply(reference)
        self.assertEqual(dict(self.connection.execute(sa.select(self.table)).mappings().one()),first)
        with self.assertRaises(CollectionDataError):
            CollectionWriter(self.settings,self.connection)._upsert(self.table,{'id':first['id']},{'title':'不能改写'})

    def test_database_rejects_invalid_status_count_and_precision(self):
        """枚举和数量精度组合约束在MySQL内实际执行。"""
        CollectionWriter(self.settings,self.connection).apply(self.source())
        for values in ({'sales_count':None}, {'sales_precision':'unknown'}, {'sales_parse_status':'failed'},
                       {'sales_metric':'bad'}, {'sales_period':'weekly'}, {'sales_count':-1}):
            with self.subTest(values=values):
                nested=self.connection.begin_nested()
                try:
                    with self.assertRaises(sa.exc.DBAPIError): self.connection.execute(self.table.update().values(**values))
                finally:
                    nested.rollback()
