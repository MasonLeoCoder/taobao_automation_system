"""商品评价的资料边界、列表分页、时间排序及媒体归属验证。"""
from datetime import datetime
from decimal import Decimal
import hashlib
import unittest

import sqlalchemy as sa

from ecommerce.modules.collection.queries import query
from ecommerce.modules.collection.review_view import review_page
from tests.unit import test_collection_browser as fixtures


def add_reviews(fixture):
    """准备两份独立评价资料；参数：隔离服务；返回：无。"""
    fixture.batch(30,'review_summary',product_id=1,captured_at=datetime(2026,10,25,10))
    fixture.put('review_summaries',batch_id=30,count_raw='1万+',positive_rate_raw='近3个月好评率高达99.6%',
        positive_rate_percent=Decimal('99.600000'),period_raw='近3个月',tags=[
            {'index':i+1,'label':f'标签{i:02d}','count_raw':'100+' if i==0 else str(i),
             'count':{'display_value':100 if i==0 else i}} for i in range(25)])
    fixture.batch(31,'reviews',product_id=1,captured_at=datetime(2026,10,25,11))
    for i in range(25):
        date=f'2026-09-{i+1:02d}' if i<24 else None
        fixture.put('review_observations',id=100+i,batch_id=31,position=i+1,author=f'用户{i:02d}',
            date_raw=date,sku_text='规格'+str(i),content='完整评价'+str(i),captured_at=datetime(2026,10,25,11),
            payload={'date':date},append_reviews=[{'content':'追评原文','time_raw':'追加评价'}] if i==23 else [])
    for module,batch in [('review_summary',30),('reviews',31)]:
        fixture.put('product_current_sections',product_id=1,section=module,module=module,batch_id=batch)


class ProductReviewTests(unittest.TestCase):
    def setUp(self):
        """建立隔离样例；参数：无；返回：无。"""
        self.fixture=fixtures.CollectionBrowserTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        add_reviews(self.fixture);self.client=self.fixture.client;self.settings=self.fixture.settings

    def view(self,**options):
        """读取合并业务视图；参数：独立页码和批次；返回：结果。"""
        data=query(self.settings,'browse_product',product_id='123')
        return review_page(self.settings,data,**options)

    def test_merged_tabs_metrics_and_original_precision(self):
        """两个旧模块入口都展示同一页面，概览数字与周期来自原资料。"""
        for url in ['/collections/products/123?module=review_summary','/collections/products/123?module=reviews',
                    '/collections/products/123?batch=30','/collections/products/123?batch=31']:
            response=self.client.get(url);self.assertEqual(response.status_code,200)
            body=response.get_data(as_text=True)
            self.assertIn('>商品评价</a>',body);self.assertNotIn('>评价概览</a>',body);self.assertNotIn('>评价明细</a>',body)
            self.assertLess(body.index('id="review-overview"'),body.index('id="review-comments"'))
            for text in ['1万+','99.6%','近3个月','概览采集时间','2026-10-25 10:00:00','2026-10-25 11:00:00','100+']:
                self.assertIn(text,body)
            self.assertNotIn('display_value',body);self.assertNotIn('count_raw',body)
        data=self.view();self.assertEqual(data['comments']['detail']['records'][0]['id'],123)
        self.assertEqual(data['comments']['detail']['total'],25)

    def test_all_tags_display_while_comments_keep_paging_and_batch_boundary(self):
        """标签超过20条仍全部展示，评论分页及批次边界不变。"""
        first=self.view()
        self.assertEqual(first['overview']['tags'][0]['count_text'],'100+')
        self.assertEqual(len(first['overview']['tags']),25)
        self.assertEqual(len(first['comments']['detail']['records']),20)
        second=self.view(page=2)
        self.assertEqual((len(second['comments']['detail']['records']),len(second['overview']['tags'])),(5,25))
        self.assertEqual(second['comments']['detail']['records'][-1]['date_text'],'未记录')
        ascending=self.view(sort_by='date',order='asc',page=2)
        self.assertEqual(ascending['comments']['detail']['records'][-1]['id'],124)
        self.fixture.batch(32,'reviews',product_id=1)
        self.fixture.put('review_observations',id=200,batch_id=32,position=1,content='另一批评论',payload={})
        chosen=self.view(reviews_batch=32)
        self.assertEqual(chosen['overview']['selected']['id'],30)
        self.assertEqual(chosen['comments']['detail']['total'],1)

    def test_foreign_module_unpublished_and_invalid_pages_are_rejected(self):
        """不能通过批次编号跨商品、跨模块或读取暂存记录。"""
        self.fixture.batch(32,'reviews',product_id=2)
        self.fixture.batch(33,'reviews',product_id=1,visibility='staging')
        for path in ['summary_batch=31','reviews_batch=30','reviews_batch=32','reviews_batch=33','page=3']:
            self.assertEqual(self.client.get('/collections/products/123?module=reviews&'+path).status_code,404,path)
        for path in ['reviews_batch=-1','sort=bad','page=0']:
            self.assertEqual(self.client.get('/collections/products/123?module=reviews&'+path).status_code,400,path)

    def test_removed_tag_controls_do_not_limit_old_links_or_escape_text(self):
        """旧标签页码和排序不再限制展示，标签原文按文本转义。"""
        table=self.fixture.store.tables['review_summaries']
        with self.fixture.engine.begin() as c:
            row=c.execute(sa.select(table).where(table.c.batch_id==30)).mappings().one()
            c.execute(table.update().where(table.c.batch_id==30).values(tags=[*row['tags'],
                {'label':'<script>标签</script>','count_raw':'0'}, {'label':'数量未知'}]))
        response=self.client.get('/collections/products/123?module=reviews&tag_page=999&tag_sort=bad&tag_order=desc')
        self.assertEqual(response.status_code,200);body=response.get_data(as_text=True)
        self.assertEqual(body.count('class="review-tag"'),27)
        self.assertIn('&lt;script&gt;标签&lt;/script&gt;',body);self.assertNotIn('<script>标签</script>',body)
        self.assertIn('class="review-tag-count">0</span>',body)
        self.assertNotIn('name="tag_page"',body);self.assertNotIn('tag_page=',body)

    def test_missing_and_confirmed_empty_modules_have_distinct_messages(self):
        """缺少一部分不隐藏另一部分，已确认无评价不伪装成从未采集。"""
        current=self.fixture.store.tables['product_current_sections']
        with self.fixture.engine.begin() as c:
            c.execute(current.delete().where(current.c.module=='reviews'))
        body=self.client.get('/collections/products/123?module=reviews').get_data(as_text=True)
        self.assertIn('99.6%',body);self.assertIn('尚无有效评价明细',body);self.assertIn('>采集明细</button>',body)
        self.fixture.batch(32,'reviews',product_id=1,status='no_reviews',coverage_status='confirmed_empty')
        self.fixture.put('product_current_sections',product_id=1,section='reviews',module='reviews',batch_id=32)
        body=self.client.get('/collections/products/123?module=reviews').get_data(as_text=True)
        self.assertIn('本次已确认无评价。',body);self.assertNotIn('尚无有效评价明细',body)

    def test_context_run_never_uses_other_runs_to_fill_missing_modules(self):
        """列表关联运行缺少概览时，不用其他时间概览填补；显式历史选择仍可查看。"""
        data=query(self.settings,'browse_product',product_id='123')
        data['source_context']={'context_run':{'id':99}}
        for row in data['history']:
            if row['id']==31: row['run_id']=99
        view=review_page(self.settings,data)
        self.assertIsNone(view['overview']['selected']);self.assertEqual(view['comments']['selected']['id'],31)
        self.assertEqual(review_page(self.settings,data,summary_batch=30)['overview']['selected']['id'],30)

    def test_review_and_append_media_are_owned_by_exact_batch_and_comment(self):
        """同商品其他评论及其他批次图片均不能串入，已保存的本地图片可访问。"""
        f=self.fixture;target=f.settings.workspace/'output/raw/review.jpg'
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'local-image')
        f.put('file_assets',id=90,root_key='workspace',relative_path='output/raw/review.jpg',availability='present',
              size_bytes=11,sha256=hashlib.sha256(b'local-image').hexdigest())
        for identifier,batch,owner,role,payload,url,file_id in [
            (90,31,123,'review_image',{},'https://img.alicdn.com/one.jpg',90),
            (91,31,123,'append_image',{'append_index':1},'https://img.alicdn.com/append.jpg',None),
            (92,31,122,'review_image',{},'https://img.alicdn.com/other.jpg',None),
            (93,30,123,'review_image',{},'https://img.alicdn.com/wrong.jpg',None),
            (94,31,123,'review_image',{},'javascript:alert(1)',None)]:
            f.put('media_references',id=identifier,batch_id=batch,review_observation_id=owner,role=role,position=identifier,
                  payload=payload,source_url=url,file_asset_id=file_id)
        data=self.view()['comments']['detail']['records'][0]
        self.assertEqual([row['id'] for row in data['media']],[90,94])
        self.assertEqual(data['media'][0]['file_id'],90);self.assertIsNone(data['media'][1]['url'])
        self.assertEqual([row['id'] for row in data['appends'][0]['media']],[91])
        body=self.client.get('/collections/products/123?module=reviews').get_data(as_text=True)
        self.assertNotIn('wrong.jpg',body);self.assertNotIn('javascript:',body)
        self.assertIn('/collections/files/90',body)
        response=self.client.get('/collections/files/90');self.assertEqual(response.status_code,200);response.close()

    def test_integer_hundred_percent_is_not_trimmed_to_one(self):
        """整数100%的显示不被末尾零处理截断。"""
        table=self.fixture.store.tables['review_summaries']
        with self.fixture.engine.begin() as c:c.execute(table.update().where(table.c.batch_id==30).values(positive_rate_percent=100))
        self.assertEqual(self.view()['overview']['rate_text'],'100%')


if __name__=='__main__':unittest.main()
