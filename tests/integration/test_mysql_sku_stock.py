"""验证库事务内核对缺货归类的派生来源、有效指针和原件留存。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
import sqlalchemy as sa

from ecommerce.shared.config import load_settings
from ecommerce.shared.sku_stock import classify_disabled_sku
from ecommerce.infrastructure.storage.collection_store import CollectionStore,CollectionWriter
from ecommerce.infrastructure.storage.collection_browser import _browse
from ecommerce.modules.collection.database_records import build_snapshot
from ecommerce.modules.collection.detail_skus import _notices
from ecommerce.modules.collection.persistence import validate_publication
from ecommerce.modules.collection.web_collection import remaining_plan


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','显式启用验证库测试')
class MySQLSkuStockTests(unittest.TestCase):
    def setUp(self):
        """准备验证库和自动回滚事务；参数：无；返回：无。"""
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=replace(load_settings(root=Path(temp.name)),task_backend='mysql',collection_backend='mysql')
        self.assertTrue(self.settings.mysql.database.endswith('_verify'))
        store=CollectionStore(self.settings);store.tasks.initialize();self.t=store.tables
        self.c=store.engine.connect();self.addCleanup(self.c.close)
        tx=self.c.begin();self.addCleanup(tx.rollback);self.task=uuid4().hex
        self.c.execute(self.t['tasks'].insert().values(id=self.task,name='example.sku_stock',origin='live',
            status='succeeded',started_at=datetime(2026,10,25,12),finished_at=datetime(2026,10,25,12)))

    def save(self,record,sections):
        """保存合成原件并调用实际快照写入器；参数：记录与分区；返回：原路径及写入结果。"""
        path=self.settings.workspace/'output/raw/taobao_details/123/history'/record['batch_id']/'sku_details.json'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(record),encoding='utf-8')
        validate_publication('skus',record,sections)
        result=CollectionWriter(self.settings,self.c).apply(build_snapshot(self.settings,'batch',record,path,module='skus'),
            {'sections':sections,'last_attempt':True})
        return path,result

    def test_rule_derived_complete_result_is_current_and_skipped_without_rewriting_old_sku(self):
        old={'schema_version':3,'product_id':'123','task_id':self.task,'batch_id':self.task+'-old',
            'captured_at':'2026-10-25T11:00:00+08:00','scope':'product_skus','status':'partial',
            'source_url':'https://item.taobao.com/item.htm?id=123','candidate_count':1,'processed_count':1,
            'groups':[{'name':'规格','options':[{'name':'替换装','value_id':'1'}]}],
            'skus':[{'index':1,'name':'替换装','stock_status':'unavailable_unconfirmed',
                'price_status':'unavailable_unconfirmed','price':None,'price_value':None,'price_reason':'旧口径',
                'unavailability_evidence':['data-disabled=true'],'image_status':'no_image','combination_confirmed':True}]}
        path,first=self.save(old,[]);original_hash=hashlib.sha256(path.read_bytes()).hexdigest()
        sku=self.t['sku_observations'];batch=self.t['collection_batches']
        before=dict(self.c.execute(sa.select(sku).where(sku.c.batch_id==first['id'])).mappings().one())
        new=deepcopy(old);new.update(batch_id=self.task+'-classified',stock_rule_source_batch_key=old['batch_id'],
            stock_rule='disabled_as_out_of_stock_v1',stock_rule_applied_at='2026-10-25T12:00:00+08:00')
        classify_disabled_sku(new['skus'][0]);new['warnings'],new['blocking_issues']=_notices(new);new['status']='complete'
        _,second=self.save(new,['sku_details'])
        row=self.c.execute(sa.select(batch).where(batch.c.id==second['id'])).mappings().one()
        self.assertEqual(row['derived_from_batch_id'],first['id']);self.assertEqual(row['derivation_kind'],'sku_stock_rule')
        self.assertEqual(row['captured_at'],datetime(2026,10,25,11))
        after=self.c.execute(sa.select(sku).where(sku.c.batch_id==second['id'])).mappings().one()
        self.assertEqual(after['stock_status'],'out_of_stock');self.assertIsNone(after['price_value']);self.assertIsNone(after['price_text'])
        self.assertEqual(before,dict(self.c.execute(sa.select(sku).where(sku.c.batch_id==first['id'])).mappings().one()))
        self.assertEqual(original_hash,hashlib.sha256(path.read_bytes()).hexdigest())
        data=_browse(self.c,self.t,'product',product_id='123');plan=remaining_plan(data)
        self.assertIn(second['id'],data['current_ids']);self.assertIn('skus',plan['skipped_modules'])
        self.assertIn('skus',data['detail_summary']['valid_modules'])


if __name__=='__main__':unittest.main()
