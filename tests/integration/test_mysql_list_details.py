"""验证库列表到详情完整关联、来源归属与约束；每例全部事务回滚。"""
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_store import CollectionStore, CollectionWriter, CollectionDataError
from ecommerce.modules.collection.database_records import build_snapshot
from ecommerce.shared.config import load_settings


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','显式启用独立MySQL关联验证')
class MySQLListDetailTests(unittest.TestCase):
    def setUp(self):
        """只连接验证库；参数：无；返回：无，所有业务样例回滚。"""
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings=replace(load_settings(root=Path(temporary.name)),task_backend='mysql',collection_backend='mysql')
        self.assertEqual(self.settings.mysql.database,'taobao_automation_verify')
        self.store=CollectionStore(self.settings)
        self.store.tasks.initialize()
        self.connection=self.store.engine.connect()
        self.transaction=self.connection.begin()
        self.addCleanup(self.connection.close)
        self.addCleanup(self.transaction.rollback)
        self.root=self.settings.workspace/'sources'
        self.root.mkdir(parents=True)
        self.task=uuid4().hex
        self.connection.execute(self.store.tables['tasks'].insert().values(id=self.task,name='example.list_detail',
            status='succeeded',started_at=datetime(2026,10,24,16),finished_at=datetime(2026,10,24,16,1),origin='live'))
        self.listing={'schema_version':4,'task_id':self.task,'batch_id':self.task+'-list','keyword':'脱敏样例',
            'search_method':'keyword','page_number':2,'search_options':None,'sort_label':'销量',
            'collection_status':'limit_reached','scope':'search_page_loaded_cards','requested_limit':2,
            'items':[{'product_id':identifier,'product_index':i,'slot_index':i,'title':'脱敏商品',
                      'price_text':'¥2','price_value':'2'} for i,identifier in enumerate(['123456','234567'],1)]}
        self.batch=self.apply('batch',self.listing,module='list')
        self.source={'keyword':'脱敏样例','search_method':'keyword','page_number':2,'sort_label':'销量','search_options':None,
            'list_source':{'source_batch_key':self.listing['batch_id'],'product_index':1,'product_id':'123456'}}

    def reference(self,kind,record,*,module=None,source=None):
        """写隔离来源并构造规范快照；参数：类型、记录与来源；返回：可重放引用。"""
        path=self.root/(uuid4().hex+'.json')
        path.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
        return build_snapshot(self.settings,kind,record,path,module=module,source=source)

    def apply(self,kind,record,*,module=None,source=None,context=None):
        """应用单快照；参数：记录、来源及发布依据；返回：数据库根记录结果。"""
        return CollectionWriter(self.settings,self.connection).apply(self.reference(kind,record,module=module,source=source),context)

    def entry(self,**changes):
        """构造可追溯详情入口；参数：可选覆盖；返回：原入口记录。"""
        return {'task_id':self.task,'product_id':'123456','entry_key':uuid4().hex,
                'status':'opened_pending_dom_review','source_url':'https://item.taobao.com/item.htm?id=123456',
                **self.source,**changes}

    def report(self,entry,**changes):
        """构造最小单模块运行；参数：入口、可选覆盖；返回：汇总原件。"""
        return {'task_id':self.task,'product_id':'123456','batch_id':uuid4().hex,'entry_key':entry['entry_key'],
            'status':'complete','steps':[{'name':'parameters','requested':True,'status':'complete','continuation_allowed':True}],**changes}

    def test_list_entry_run_module_chain_replays_without_duplicates(self):
        """列表观察到入口、运行和参数批次完整关联，同一快照重复应用不重复建行。"""
        entry=self.entry()
        reference=self.reference('page',entry,module='detail_open')
        first=CollectionWriter(self.settings,self.connection).apply(reference)
        second=CollectionWriter(self.settings,self.connection).apply(reference)
        self.assertEqual(first['id'],second['id'])
        report=self.report(entry)
        run=self.apply('report',report)
        parameters={'task_id':self.task,'product_id':'123456','batch_id':uuid4().hex,'status':'complete',
            'items':[{'index':1,'name':'品牌','value':'脱敏品牌','status':'read'}]}
        detail=self.apply('batch',parameters,module='parameters',source=self.source,context={'report_key':report['batch_id'],'sections':['parameters']})
        tables=self.store.tables
        page=self.connection.execute(sa.select(tables['search_records']).where(tables['search_records'].c.id==first['id'])).mappings().one()
        observation=self.connection.execute(sa.select(tables['list_observations']).where(tables['list_observations'].c.id==page['source_list_observation_id'])).mappings().one()
        self.assertEqual((observation['batch_id'],observation['product_index']),(self.batch['id'],1))
        self.assertEqual(self.connection.scalar(sa.select(tables['collection_runs'].c.entry_record_id).where(tables['collection_runs'].c.id==run['id'])),first['id'])
        batch=self.connection.execute(sa.select(tables['collection_batches']).where(tables['collection_batches'].c.id==detail['id'])).mappings().one()
        self.assertEqual((batch['run_id'],batch['search_method'],batch['search_keyword'],batch['page_number']),(run['id'],'keyword','脱敏样例',2))
        with self.assertRaises(CollectionDataError):
            self.apply('batch',dict(parameters,batch_id=uuid4().hex),module='parameters',
                source=dict(self.source,keyword='错误来源'),context={'report_key':report['batch_id']})

    def test_wrong_product_task_and_search_origin_are_rejected(self):
        """存在的编号也必须符合商品、任务和关键词归属，错误来源全部拒绝。"""
        for overrides in [{'product_id':'234567'}, {'keyword':'错误词'}, {'task_id':None},
                          {'list_source':dict(self.source['list_source'],product_index=2)}]:
            with self.subTest(overrides=overrides),self.connection.begin_nested():
                with self.assertRaises(CollectionDataError):
                    self.apply('page',self.entry(**overrides),module='detail_open')
        entry=self.entry()
        self.apply('page',entry,module='detail_open')
        with self.assertRaises(CollectionDataError):
            self.apply('report',self.report(entry,product_id='234567'))

    def test_foreign_keys_restrict_deletion_and_check_rejects_search_link(self):
        """原生外键禁止删除被引用观察，搜索记录不能冒充详情入口。"""
        result=self.apply('page',self.entry(),module='detail_open')
        pages=self.store.tables['search_records']
        identifier=self.connection.scalar(sa.select(pages.c.source_list_observation_id).where(pages.c.id==result['id']))
        transaction=self.connection.begin_nested()
        with self.assertRaises(sa.exc.IntegrityError):
            self.connection.execute(self.store.tables['list_observations'].delete().where(self.store.tables['list_observations'].c.id==identifier))
        transaction.rollback()
        transaction=self.connection.begin_nested()
        with self.assertRaises(sa.exc.OperationalError) as rejected:
            self.connection.execute(pages.insert().values(record_type='search',status='results',source_list_observation_id=identifier))
        self.assertEqual(rejected.exception.orig.args[0],3819)
        transaction.rollback()

    def test_standalone_old_entry_and_report_keep_nullable_new_columns(self):
        """旧入口无来源，旧汇总无入口，兼容写入时两列保持NULL。"""
        entry=self.entry()
        for key in [*self.source,'entry_key']:
            entry.pop(key,None)
        page=self.apply('page',entry,module='detail_open')
        report=self.report({'entry_key':'unused'})
        report.pop('entry_key')
        run=self.apply('report',report)
        tables=self.store.tables
        self.assertIsNone(self.connection.scalar(sa.select(tables['search_records'].c.source_list_observation_id).where(tables['search_records'].c.id==page['id'])))
        self.assertIsNone(self.connection.scalar(sa.select(tables['collection_runs'].c.entry_record_id).where(tables['collection_runs'].c.id==run['id'])))
