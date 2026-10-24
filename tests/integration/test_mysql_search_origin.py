"""验证库来源写入、旧快照重放及图片文件关联；每例事务回滚。"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage.collection_store import CollectionStore,CollectionWriter,CollectionDataError
from ecommerce.infrastructure.storage.collection_snapshot import open_snapshot
from ecommerce.modules.collection.database_records import build_snapshot
from ecommerce.shared.config import load_settings


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','显式启用独立MySQL来源测试')
class MySQLSearchOriginTests(unittest.TestCase):
    def setUp(self):
        """只连接验证库，创建独立临时原件及可回滚事务。"""
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings=replace(load_settings(root=Path(temporary.name)),collection_backend='mysql',task_backend='mysql')
        self.assertEqual(self.settings.mysql.database,'taobao_automation_verify')
        self.store=CollectionStore(self.settings)
        self.store.tasks.initialize()
        self.connection=self.store.engine.connect()
        self.transaction=self.connection.begin()
        self.addCleanup(self.connection.close)
        self.addCleanup(self.transaction.rollback)
        self.root=self.settings.workspace/'origin_test'
        self.root.mkdir(parents=True)

    def snapshot(self,kind,record):
        """生成真实规范快照；参数：记录类型、脱敏来源；返回：带哈希引用。"""
        path=self.root/(uuid4().hex+'.json')
        path.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
        return build_snapshot(self.settings,kind,record,path,module='list' if kind=='batch' else 'search')

    def listing(self,keyword='样例'):
        """生成新格式限定列表；参数：搜索词；返回：独立原批次记录。"""
        return {'schema_version':3,'search_method':'keyword','keyword':keyword,'page_number':1,
            'search_options':None,'batch_id':uuid4().hex,'captured_at':'2026-10-23T20:00:00+08:00',
            'scope':'first_page_loaded_cards','collection_status':'limit_reached','requested_limit':1,
            'sort_label':'销量','items':[{'slot_index':1,'product_index':1,'product_id':'12345678901',
                'title':'脱敏来源样例','price_text':'¥2 首单价','price_value':'2','price_label':'首单价'}]}

    def test_new_keyword_batch_and_page_fill_independent_fields(self):
        """新来源分别写入批次和独立搜索，不强建两者关系。"""
        listing=self.listing()
        reference=self.snapshot('batch',listing)
        result=CollectionWriter(self.settings,self.connection).apply(reference)
        table=self.store.tables['collection_batches']
        row=self.connection.execute(sa.select(table).where(table.c.id==result['id'])).mappings().one()
        self.assertEqual((row['search_method'],row['search_keyword'],row['page_number']),('keyword','样例',1))
        self.assertIsNone(row['search_image_file_id'])
        self.assertTrue(self.connection.scalar(sa.select(table.c.search_options.is_(None)).where(table.c.id==result['id'])))
        self.assertEqual(row['result_payload']['scope_contract']['version'],2)
        page={'schema_version':3,'search_method':'keyword','keyword':'另一个词','status':'results',
              'checked_at':'2026-10-23T20:00:01+08:00'}
        result=CollectionWriter(self.settings,self.connection).apply(self.snapshot('page',page))
        table=self.store.tables['search_records']
        row=self.connection.execute(sa.select(table).where(table.c.id==result['id'])).mappings().one()
        self.assertEqual((row['search_method'],row['keyword']),('keyword','另一个词'))
        self.assertIsNone(row['page_number'])

    def test_legacy_reference_and_rebuilt_snapshot_replay_preserve_backfill(self):
        """旧原件生成的快照哈希不变，回填后重放不清空新列或新增商品观察。"""
        record=self.listing()
        record.pop('search_method')
        record.pop('search_options')
        record['schema_version']=2
        reference=self.snapshot('batch',record)
        header,_=open_snapshot(self.settings,reference)
        self.assertNotIn('search_method',header['values'])
        result=CollectionWriter(self.settings,self.connection).apply(reference)
        table=self.store.tables['collection_batches']
        original=dict(self.connection.execute(sa.select(table).where(table.c.id==result['id'])).mappings().one())
        self.assertEqual(original['search_method'],'keyword')
        CollectionWriter(self.settings,self.connection).apply(reference)
        rebuilt=build_snapshot(self.settings,'batch',record,self.root/header['source_relative_path'].split('/')[-1],module='list')
        self.assertEqual(reference['sha256'],rebuilt['sha256'])
        CollectionWriter(self.settings,self.connection).apply(rebuilt)
        after=dict(self.connection.execute(sa.select(table).where(table.c.id==result['id'])).mappings().one())
        self.assertEqual(original,after)
        observations=self.store.tables['list_observations']
        self.assertEqual(self.connection.scalar(sa.select(sa.func.count()).select_from(observations).where(observations.c.batch_id==result['id'])),1)
        changed={**record,'keyword':'不同的来源'}
        with self.assertRaisesRegex(CollectionDataError,'冲突'):
            CollectionWriter(self.settings,self.connection).apply(self.snapshot('batch',changed))

    def test_synthetic_image_origin_uses_actual_input_file_and_restricts_delete(self):
        """只验证图片数据契约，文件外键指向实际输入图且不同图范围不同；不启动平台搜索。"""
        ids=[]
        for content in (b'synthetic-input-image-A',b'synthetic-input-image-B'):
            image=self.root/(uuid4().hex+'.png')
            image.write_bytes(content)
            record=self.listing()
            record.update(search_method='image',keyword=None,
                search_image={'path':str(image),'sha256':hashlib.sha256(content).hexdigest(),'content_type':'image/png'})
            result=CollectionWriter(self.settings,self.connection).apply(self.snapshot('batch',record))
            table=self.store.tables['collection_batches']
            row=self.connection.execute(sa.select(table).where(table.c.id==result['id'])).mappings().one()
            self.assertEqual(row['search_method'],'image')
            self.assertIsNone(row['search_keyword'])
            self.assertIsNotNone(row['search_image_file_id'])
            ids.append((row['scope_key'],row['search_image_file_id']))
            files=self.store.tables['file_assets']
            self.assertEqual(self.connection.scalar(sa.select(files.c.sha256).where(files.c.id==row['search_image_file_id'])),record['search_image']['sha256'])
            with self.connection.begin_nested():
                with self.assertRaises(sa.exc.IntegrityError):
                    self.connection.execute(files.delete().where(files.c.id==row['search_image_file_id']))
        self.assertNotEqual(ids[0][0],ids[1][0])
        self.assertNotEqual(ids[0][1],ids[1][1])

    def test_database_rejects_invalid_method_page_keyword_and_file(self):
        """数据库自身拒绝非法来源代码、页码、缺词和不存在文件编号。"""
        table=self.store.tables['collection_batches']
        base={'platform':'taobao','module':'list','status':'empty','scope_key':'d'*64,
              'coverage_status':'confirmed_empty','visibility':'published'}
        for fields in ({'search_method':'bad'},{'page_number':0},{'search_method':'keyword'},
                       {'search_method':'image','search_image_file_id':999999999999},
                       {'search_method':None,'search_image_file_id':999999999999}):
            with self.subTest(fields=fields),self.connection.begin_nested():
                with self.assertRaises(sa.exc.DBAPIError) as caught:
                    self.connection.execute(table.insert().values(**base,source_batch_key=uuid4().hex,**fields))
                self.assertIn(caught.exception.orig.args[0],(3819,1452))
