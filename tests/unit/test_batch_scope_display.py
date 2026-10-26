"""批次状态展示的数量、来源和分页边界验证；使用临时库。"""
import unittest

import sqlalchemy as sa

from ecommerce.infrastructure.storage.batch_counts import attach_batch_counts
from ecommerce.infrastructure.storage.collection_browser import browse
from ecommerce.infrastructure.storage.collection_queries import records, batches
from ecommerce.web.batch_presentation import batch_scope_text
from tests.unit import test_collection_browser as fixtures
from tests.unit.test_product_reviews import add_reviews


class BatchScopeDisplayTests(unittest.TestCase):
    def setUp(self):
        """建立隔离样例；参数：无；返回：无。"""
        self.fixture=fixtures.CollectionBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_counts_are_batch_totals_not_page_or_advertised_numbers(self):
        """评论翻页仍显示本批25条；标签数量来自数组，不取平台1万+。"""
        f=self.fixture
        add_reviews(f)
        first=records(f.settings,31,page=1)
        second=records(f.settings,31,page=2)
        self.assertEqual((len(first['records']),len(second['records'])),(20,5))
        self.assertEqual(first['batch']['record_counts'],{'records':25})
        self.assertEqual(second['batch']['record_counts'],{'records':25})
        history=browse(f.settings,'product',product_id='123')['history']
        summary=next(row for row in history if row['id']==30)
        self.assertEqual(batch_scope_text(summary),'25个评价标签')
        self.assertEqual(summary['coverage_status'],'unknown')
        queried=batches(f.settings,module='reviews')['batches'][0]
        self.assertEqual(queried['record_counts'],first['batch']['record_counts'])
        body=f.client.get('/collections/products/123?module=reviews&page=2').get_data(as_text=True)
        self.assertIn('25条评价',body)
        self.assertIn('25个评价标签',body)
        self.assertNotIn('未确认覆盖范围',body)

    def test_question_answers_require_matching_owner_and_batch(self):
        """只数同批实际问题及其回答，排除孤立和跨批回答。"""
        f=self.fixture
        for identifier in [30,31]:
            f.batch(identifier,'questions',product_id=1,coverage_status='visible_end_confirmed',collection_mode='all')
        f.put('question_observations',id=300,batch_id=30,question='问题')
        f.put('question_observations',id=301,batch_id=30,question='无回答')
        f.put('question_observations',id=302,batch_id=31,question='另批问题')
        for identifier,batch,question in [(1,30,300),(2,30,300),(3,31,300),(4,30,302),(5,30,999)]:
            f.put('answer_observations',id=identifier,batch_id=batch,question_id=question,content='回答')
        data=records(f.settings,30,page=1)
        self.assertEqual(data['batch']['record_counts'],{'records':2,'answers':2})
        text=batch_scope_text(data['batch'])
        self.assertIn('2个问题、2条回答',text)
        self.assertIn('已到达可见末尾',text)

    def test_media_uses_module_roles_and_skus_count_confirmed_stock_only(self):
        """导出文件、封面、他批图片不计数；明确缺货与未知不可选不混淆。"""
        f=self.fixture
        f.batch(30,'main_images',product_id=1)
        f.batch(31,'detail_images',product_id=1)
        f.batch(32,'skus',product_id=1)
        for identifier,batch,role in [(1,30,'main_image'),(2,30,'main_image'),(3,30,'sku_image'),
                                      (4,30,'main_video'),(5,31,'detail_image')]:
            f.put('media_references',id=identifier,batch_id=batch,role=role)
        for identifier,status in [(1,'selectable'),(2,'out_of_stock'),(3,'unavailable_unconfirmed')]:
            f.put('sku_observations',id=identifier,batch_id=32,stock_status=status)
        data=browse(f.settings,'product',product_id='123')
        rows={row['id']:row for row in data['history']}
        self.assertEqual(batch_scope_text(rows[30]),'2张图片')
        self.assertEqual(batch_scope_text(rows[31]),'1张图片')
        self.assertEqual(batch_scope_text(rows[32]),'3种规格，其中1种缺货')

    def test_missing_counts_are_omitted_and_empty_tags_are_distinct(self):
        """无明细不臆造0，已存空标签数组为0；标签未记录则不推断。"""
        f=self.fixture
        for identifier in [30,31,32]:
            f.batch(identifier,'review_summary',product_id=1)
        f.put('review_summaries',batch_id=30,tags=[])
        f.put('review_summaries',batch_id=31,tags=None)
        self.assertEqual(batch_scope_text(records(f.settings,30)['batch']),'0个评价标签')
        for identifier in [31,32]:
            self.assertEqual(batch_scope_text(records(f.settings,identifier)['batch']),'')
        self.assertEqual(records(f.settings,21)['batch']['record_counts'],{})
        body=f.client.get('/collections/batches/21').get_data(as_text=True)
        self.assertIn('部分完成',body)
        self.assertNotIn('0项参数',body)

    def test_state_is_not_promoted_by_saved_rows_and_empty_is_explicit(self):
        """失败或部分完成即使有数据也不变为完成，无内容与尚未采集有别。"""
        f=self.fixture
        for identifier,state in [(30,'partial'),(31,'failed'),(32,'needs_attention')]:
            f.batch(identifier,'parameters',product_id=1,status=state,coverage_status='incomplete')
            f.put('parameter_observations',id=identifier,batch_id=identifier,name='名称',value='值')
            data=records(f.settings,identifier)['batch']
            self.assertEqual(data['status'],state)
            self.assertEqual(batch_scope_text(data),'1项参数')
        f.batch(33,'video',product_id=1,status='no_video',coverage_status='confirmed_empty')
        body=f.client.get('/collections/batches/33').get_data(as_text=True)
        self.assertIn('已确认无视频',body)
        self.assertNotIn('0个视频',body)
        self.assertNotIn('未确认覆盖范围',body)

    def test_scope_hides_unknown_only_for_fixed_modules_and_keeps_known_limits(self):
        """固定模块隐藏通用unknown，评价问答仍明确未记录范围并保留上限。"""
        for module in ['parameters','main_images','detail_images','review_summary','skus','video','basic_info']:
            self.assertEqual(batch_scope_text({'module':module,'status':'complete','coverage_status':'unknown'}),'')
        for module in ['reviews','questions','list']:
            self.assertEqual(batch_scope_text({'module':module,'status':'complete','coverage_status':'unknown'}),'采集范围未记录')
        text=batch_scope_text({'module':'reviews','status':'complete','coverage_status':'limit_reached',
                              'collection_mode':'limited','requested_limit':100,'record_counts':{'records':100}})
        self.assertEqual(text,'100条评价 · 已达到本次上限 · 本次上限 100')
        text=batch_scope_text({'module':'list','status':'limit_reached','coverage_status':'limit_reached',
                              'record_counts':{'records':3}})
        self.assertEqual(text,'3条列表记录')
        text=batch_scope_text({'module':'parameters','status':'complete','coverage_status':'manual_count_confirmed'})
        self.assertEqual(text,'已人工确认采集范围')

    def test_grouped_counts_only_select_and_leave_source_unchanged(self):
        """同模块多批只发一次计数查询，原状态及范围不回写。"""
        f=self.fixture
        with f.engine.connect() as connection:
            table=f.store.tables['collection_batches']
            rows=[dict(row) for row in connection.execute(sa.select(table).where(table.c.module=='parameters')).mappings()]
            statements=[]
            def capture(conn,cursor,statement,parameters,context,executemany):
                """记录隔离库SQL；参数：SQL事件；返回：无。"""
                statements.append(statement)
            sa.event.listen(f.engine,'before_cursor_execute',capture)
            try:
                attach_batch_counts(connection,f.store.tables,rows)
            finally:
                sa.event.remove(f.engine,'before_cursor_execute',capture)
            self.assertEqual(len(statements),1)
            self.assertTrue(statements[0].lstrip().upper().startswith('SELECT'))
            saved=connection.execute(sa.select(table.c.status,table.c.coverage_status).where(table.c.id==20)).one()
            self.assertEqual(tuple(saved),('complete','unknown'))
        for path in ['/collections/products/123?module=history','/collections/products/123',
                     '/collections/products/123?module=parameters','/collections/batches/20']:
            response=f.client.get(path)
            self.assertEqual(response.status_code,200,path)
            self.assertIn('2项参数',response.get_data(as_text=True),path)
            self.assertNotIn('未确认覆盖范围',response.get_data(as_text=True),path)
