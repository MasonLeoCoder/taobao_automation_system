"""搜索来源的证据解释、旧快照兼容和新范围身份测试。"""
import unittest

from ecommerce.infrastructure.storage.mysql_migrations.search_origin_v3 import origin_values, enrich_origin
from ecommerce.infrastructure.storage.mysql_migrations import schema, schema_v3
from ecommerce.modules.collection.database_records import scope_contract


class SearchOriginTests(unittest.TestCase):
    """使用脱敏字典验证来源语义，不访问正式平台或数据库。"""

    def test_keyword_source_is_independent_of_product_title(self):
        """已知搜索流程使用真实输入词，详情入口不从标题推测来源。"""
        values=origin_values('collection_batches','list',{'keyword':'样例词','page_number':2})
        self.assertEqual((values['search_method'],values['search_keyword'],values['page_number']),('keyword','样例词',2))
        detail=origin_values('search_records','detail_open',{'title':'样例词'})
        self.assertEqual(detail,{'search_method':None,'search_image_file_id':None})

    def test_missing_search_input_remains_unknown(self):
        """确认搜索但输入不明时，不制造关键词；缺少页码不默认第1页。"""
        values=origin_values('collection_batches','list',{})
        self.assertEqual(values['search_method'],'unknown')
        self.assertIsNone(values['search_keyword'])
        self.assertIsNone(values['page_number'])
        self.assertIsNone(values['search_options'])

    def test_explicit_keyword_and_url_must_agree(self):
        """明确输入与来源链接矛盾时拒绝写入，不任意选择一方。"""
        with self.assertRaisesRegex(ValueError,'冲突'):
            origin_values('collection_batches','list',{'search_method':'keyword','keyword':'甲'},'https://s.taobao.com/search?q=乙')
        with self.assertRaisesRegex(ValueError,'缺少'):
            origin_values('search_records','search',{'search_method':'keyword'})

    def test_invalid_options_page_and_method_are_rejected(self):
        """非法方式、非整数页码及非对象条件均不能静默入库。"""
        for record in ({'search_method':'bad'},{'page_number':True},{'page_number':0},
                       {'page_number':'1'},{'search_options':[]},{'search_options':{'price':float('nan')}}):
            with self.subTest(record=record),self.assertRaises(ValueError):
                origin_values('collection_batches','list',record)

    def test_old_snapshot_does_not_clear_confirmed_origin(self):
        """旧字段缺失不等于明确空值，补写保持已回填的来源。"""
        row={'module':'list','result_payload':{'record':{}},'search_method':'keyword',
             'search_keyword':'已有词','page_number':2,'search_options':None,'search_image_file_id':None}
        empty=enrich_origin('collection_batches',{'module':'list','result_payload':{'record':{}}},row)
        self.assertEqual((empty['search_method'],empty['search_keyword'],empty['page_number']),('keyword','已有词',2))
        row['result_payload']={'record':{'keyword':'已有词'}}
        values=enrich_origin('collection_batches',{'module':'list','result_payload':row['result_payload']},row)
        self.assertEqual(values['page_number'],2)
        self.assertEqual(values['search_method'],'keyword')

    def test_origin_columns_cannot_disagree_with_evidence(self):
        """新增独立列与原始输入不一致时拒绝写入。"""
        with self.assertRaisesRegex(ValueError,'冲突'):
            enrich_origin('collection_batches',{'module':'list','search_keyword':'乙',
                'result_payload':{'record':{'keyword':'甲'}}})

    def test_legacy_scope_contract_remains_version_one(self):
        """旧原件仍使用原范围契约，不因增加列改变旧快照哈希输入。"""
        record={'keyword':'样例','page_number':1,'requested_limit':5,'scope':'first_page_loaded_cards'}
        _,contract,_=scope_contract('list',record)
        self.assertEqual(contract['version'],1)
        self.assertNotIn('search_method',contract)
        self.assertNotIn('search_image_sha256',contract)

    def test_new_scope_distinguishes_methods_images_and_options(self):
        """不同方式、输入图或筛选条件不能因关键词为空而合并范围。"""
        record={'search_method':'image','search_image':{'sha256':'a'*64}}
        first=scope_contract('list',record)[0]
        second=scope_contract('list',{**record,'search_image':{'sha256':'b'*64}})[0]
        filtered=scope_contract('list',{**record,'search_options':{'category':'样例'}})[0]
        self.assertEqual(len({first,second,filtered}),3)
        with self.assertRaisesRegex(ValueError,'SHA-256'):
            scope_contract('list',{'search_method':'image'})

    def test_new_model_keeps_frozen_time_migration_model_intact(self):
        """旧时间迁移仍指向固定版本，新模型仅增加指定7列和2个外键。"""
        old,new=schema.build_metadata(),schema_v3.build_metadata()
        self.assertEqual(schema.REVISION,'0002_beijing_time')
        self.assertEqual(sum(len(t.c) for t in new.tables.values())-sum(len(t.c) for t in old.tables.values()),7)
        self.assertEqual(sum(len(t.foreign_key_constraints) for t in new.tables.values())-
                         sum(len(t.foreign_key_constraints) for t in old.tables.values()),2)
        for name in old.tables:
            self.assertTrue(set(old.tables[name].c.keys()) <= set(new.tables[name].c.keys()))
        self.assertNotIn('search_method',old.tables['collection_batches'].c)
