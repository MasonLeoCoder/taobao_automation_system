"""商品库详情入口来源的证据与归属边界验证；使用临时数据库。"""
from datetime import datetime, timedelta
import unittest

from ecommerce.modules.collection.queries import query
from tests.unit import test_collection_browser as fixtures


class DetailEntrySourceTests(unittest.TestCase):
    def setUp(self):
        """建立直接入口任务及临时库；参数：无；返回：无。"""
        self.f=fixtures.CollectionBrowserTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.direct_task='d'*32
        self.f.put('tasks',id=self.direct_task,name='collection.taobao.detail.open',status='succeeded',started_at=self.f.now)

    def row(self,product='123'):
        """查询一件商品；参数：平台编号；返回：商品库行。"""
        return query(self.f.settings,'browse_catalog',page=1,keyword=product)['products'][0]

    def entry(self,identifier,*,product_id=1,platform_id='123',task=None,source=None,captured_at=None,**extra):
        """登记脱敏入口证据；参数：归属、输入及时间；返回：无。"""
        task=task or self.direct_task
        values=dict(id=identifier,product_id=product_id,task_id=task,record_type='detail_open',
            captured_at=captured_at or self.f.now,source_list_observation_id=source,
            payload={'record':{'task_id':task,'product_id':platform_id,'input_type':'id'}})
        values.update(extra)
        self.f.put('search_records',**values)

    def add_run(self,identifier,entry_id,*,product_id=1,task=None,started_at=None):
        """登记运行及明确入口关联；参数：归属和时间；返回：无。"""
        self.f.put('collection_runs',id=identifier,product_id=product_id,task_id=task or self.direct_task,
            entry_record_id=entry_id,status='complete',started_at=started_at or self.f.now+timedelta(seconds=1))

    def test_direct_entry_does_not_change_historical_list_association(self):
        """已有搜索词的商品直接采集，资料类型仍为两者都有，入口独立显示直接。"""
        self.entry(30);self.add_run(30,30)
        row=self.row()
        self.assertEqual((row['origin'],row['detail_entry_kind'],row['detail_entry_label']),('both','direct','直接采集'))
        self.assertEqual(row['search_keywords'],['粘毛器'])
        self.assertIsNone(row['detail_entry_keyword'])
        self.assertEqual(row['detail_entry_id'],30)
        self.entry(31,product_id=2,platform_id='456');self.add_run(31,31,product_id=2)
        only=self.row('456')
        self.assertEqual((only['origin'],only['detail_entry_kind'],only['search_keywords']),('detail','direct',[]))

    def test_search_entry_requires_same_product_and_task_published_list(self):
        """实际列表入口按关联记录核对，不用输入url区分直接和搜索。"""
        self.entry(30,task=self.f.task,source=11,keyword='粘毛器',
            payload={'record':{'input_type':'url','list_source':{'product_id':'123'}}})
        self.add_run(30,30,task=self.f.task)
        row=self.row()
        self.assertEqual((row['detail_entry_kind'],row['detail_entry_keyword']),('search','粘毛器'))
        self.assertEqual(row['detail_entry_label'],'搜索列表进入')
        e=self.f.store.tables['search_records']
        for values in [{'source_list_observation_id':999},{'source_list_observation_id':11,'keyword':'其他词'},
                       {'source_list_observation_id':11,'keyword':'粘毛器','task_id':self.direct_task}]:
            with self.f.engine.begin() as c:c.execute(e.update().where(e.c.id==30).values(**values))
            self.assertEqual(self.row()['detail_entry_kind'],'unknown')

    def test_null_source_and_input_type_alone_do_not_prove_direct_entry(self):
        """NULL可表示旧来源缺失，只有ID或URL输入也不足以判断直接采集。"""
        self.entry(30,task=self.f.task)
        self.add_run(30,30,task=self.f.task)
        self.assertEqual(self.row()['detail_entry_kind'],'unknown')

    def test_new_run_without_entry_never_borrows_old_confirmed_entry(self):
        """最新运行缺关联时保持未知，不用较早直接入口填补。"""
        self.entry(30);self.add_run(30,30)
        self.add_run(31,None,started_at=self.f.now+timedelta(minutes=1))
        row=self.row()
        self.assertEqual((row['detail_entry_kind'],row['detail_entry_id']),('unknown',None))

    def test_newer_open_entry_can_display_before_a_run_exists(self):
        """运行后另有明确新入口，显示新入口及时间；仅打开也不提升模块数。"""
        self.entry(30,task=self.f.task,source=11,keyword='粘毛器');self.add_run(30,30,task=self.f.task)
        self.entry(31,captured_at=self.f.now+timedelta(minutes=1))
        row=self.row()
        self.assertEqual((row['detail_entry_kind'],row['detail_entry_id'],row['valid_count']),('direct',31,1))

    def test_cross_product_entry_and_non_detail_type_are_unknown(self):
        """运行指向他商品或非详情入口，不显示错误来源。"""
        self.entry(30,product_id=2,platform_id='456');self.add_run(30,30)
        self.assertEqual(self.row()['detail_entry_kind'],'unknown')
        e=self.f.store.tables['search_records']
        with self.f.engine.begin() as c:
            c.execute(e.update().where(e.c.id==30).values(product_id=1,record_type='search'))
        self.assertEqual(self.row()['detail_entry_kind'],'unknown')

    def test_missing_or_conflicting_raw_input_is_not_labeled_direct(self):
        """独立任务仍需可核对原始输入，不把冲突记录强行归类。"""
        self.entry(30);self.add_run(30,30)
        e=self.f.store.tables['search_records']
        for record in [{},{'task_id':self.direct_task,'product_id':'456','input_type':'id'},
                       {'task_id':self.f.task,'product_id':'123','input_type':'url'},
                       {'task_id':self.direct_task,'product_id':'123','input_type':'id','list_source':{}}]:
            with self.f.engine.begin() as c:c.execute(e.update().where(e.c.id==30).values(payload={'record':record}))
            self.assertEqual(self.row()['detail_entry_kind'],'unknown')

    def test_list_only_and_registered_have_no_detail_record(self):
        """未开始详情采集不提示来源丢失。"""
        self.f.put('products',id=3,platform='taobao',platform_product_id='789')
        self.f.put('list_observations',id=30,batch_id=11,product_id=3,slot_index=3)
        for product in ['456','789']:
            row=self.row(product)
            self.assertEqual((row['detail_entry_kind'],row['detail_entry_label']),('not_started','尚无详情记录'))
        self.assertEqual(self.row()['detail_entry_label'],'入口未记录')
