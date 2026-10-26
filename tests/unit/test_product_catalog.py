"""全部商品库的来源、快照、筛选和空缺边界验证；只用临时库。"""
from datetime import datetime
import hashlib
import unittest

from ecommerce.modules.collection.queries import query
from ecommerce.modules.collection.web_collection import remaining_plan
from tests.unit import test_collection_browser as fixtures


class ProductCatalogTests(unittest.TestCase):
    def setUp(self):
        """建立临时来源记录；参数：无；返回：无。"""
        self.f=fixtures.CollectionBrowserTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.put=self.f.put

    def catalog(self,**options):
        """读取商品库；参数：筛选及分页；返回：查询结果。"""
        return query(self.f.settings,'browse_catalog',page=options.pop('page',1),**options)

    def product(self,identifier,platform_id,title='同名商品',platform='taobao'):
        """登记商品；参数：内部和外部编号、标题、平台；返回：无。"""
        self.put('products',id=identifier,platform=platform,platform_product_id=platform_id,display_title=title)

    def test_all_registered_products_open_and_staging_data_stays_hidden(self):
        """列表商品、仅登记及仅暂存身份都可打开，暂存内容仍不可见。"""
        self.product(3,'789')
        self.product(4,'999',platform='qianniu')
        data=self.catalog()
        self.assertEqual({row['platform_product_id'] for row in data['products']},{'123','456','789'})
        registered=self.catalog(origin='registered')['products']
        self.assertEqual({row['platform_product_id'] for row in registered},{'456','789'})
        for row in registered:
            self.assertEqual(row['catalog_state'],'registered')
            self.assertEqual(row['valid_count'],0)
            self.assertIsNone(row['captured_at'])
            self.assertIsNone(row['thumbnail'])
            detail=query(self.f.settings,'browse_product',product_id=row['platform_product_id'])
            self.assertEqual(detail['history'],[])
            self.assertEqual(len(remaining_plan(detail)['modules']),9)
            self.assertEqual(self.f.client.get('/collections/products/'+row['platform_product_id']).status_code,200)
        self.assertEqual(self.f.client.get('/collections/products/000999').status_code,404)

    def test_latest_list_is_one_record_and_all_keywords_stay_traceable(self):
        """筛选历史搜索词不改变显示快照，旧价格、店铺不拼接到最新记录。"""
        b,l=self.f.store.tables['collection_batches'],self.f.store.tables['list_observations']
        with self.f.engine.begin() as c:
            c.execute(b.update().where(b.c.id==10).values(search_keyword='旧词',captured_at=datetime(2026,10,24)))
            c.execute(b.update().where(b.c.id==11).values(search_keyword='新词'))
            c.execute(l.update().where(l.c.id==10).values(price_text='99元',price_value=99,sales_text='1000+人收货',sales_count=1000,shop_name='旧店'))
            c.execute(l.update().where(l.c.id==11).values(price_text='9元',price_value=9,sales_text='0人收货',sales_count=0,shop_name='新店'))
        row=self.catalog(source='旧词')['products'][0]
        self.assertEqual((row['list_observation_id'],row['price_text'],row['sales_text'],row['sales_count'],row['shop_name']),(11,'9元','0人收货',0,'新店'))
        self.assertEqual(set(row['search_keywords']),{'旧词','新词'})
        self.assertEqual(self.catalog(shop='旧店')['total'],0)
        self.assertEqual(self.catalog(shop='新店')['total'],1)
        self.assertEqual(self.catalog(sales_min='0',sales_max='0')['total'],1)
        self.assertEqual(self.catalog(sales_min='1')['total'],0)
        self.assertEqual(row['origin'],'both')

    def test_detail_only_uses_basic_info_and_preserves_raw_price_and_sales(self):
        """详情独有商品使用自身基本信息，原始区间价格与未知数量不变为0。"""
        self.product(3,'789')
        self.f.batch(30,'basic_info',product_id=3)
        self.put('product_info_observations',id=30,batch_id=30,product_id=3,module='basic_info',
                 title='详情标题',price_text='¥3.5起',sales_text='已售10万+',shop_name='详情店',captured_at=self.f.now)
        row=self.catalog(origin='detail')['products'][0]
        self.assertEqual((row['price_text'],row['sales_text'],row['shop_name'],row['value_source']),('¥3.5起','已售10万+','详情店','详情页'))
        self.assertIsNone(row['sales_count'])
        self.assertIsNone(row['price_value'])
        self.assertEqual(self.catalog(sales_max='0',origin='detail')['total'],0)
        self.assertEqual(self.catalog(shop='详情店')['total'],1)
        self.f.batch(31,'basic_info',product_id=3,status='partial')
        self.put('product_info_observations',id=31,batch_id=31,product_id=3,module='basic_info',price_text='不完整价格')
        self.assertEqual(self.catalog(origin='detail')['products'][0]['price_text'],'¥3.5起')

    def test_recent_record_compares_sources_and_date_filter_uses_same_column(self):
        """较早详情尝试不压过较新列表记录，时间缺失排在后面。"""
        self.put('collection_runs',id=30,product_id=1,status='failed',started_at=datetime(2026,10,20),task_id=self.f.task)
        rows=self.catalog()['products']
        self.assertEqual(rows[0]['platform_product_id'],'123')
        self.assertEqual(datetime.fromisoformat(rows[0]['captured_at']).replace(tzinfo=None),self.f.now)
        self.assertEqual(rows[0]['run_label'],'失败')
        self.assertEqual(rows[-1]['platform_product_id'],'456')
        self.assertEqual(self.catalog(since='2026-10-25',until='2026-10-25')['total'],1)
        self.assertEqual(self.catalog(until='2026-10-24')['total'],0)

    def test_valid_modules_and_last_failed_run_are_independent(self):
        """失败保留已有有效分区，明确无内容也算模块完成。"""
        self.put('collection_runs',id=30,product_id=1,status='failed',started_at=self.f.now,task_id=self.f.task)
        row=self.catalog(state='partial')['products'][0]
        self.assertEqual((row['valid_count'],row['remaining_count'],row['run_status']),(1,8,'failed'))
        self.product(3,'789')
        modules=['basic_info','video','main_images','skus','parameters','detail_images','review_summary','reviews','questions']
        for index,module in enumerate(modules,100):
            self.f.batch(index,module,product_id=3,status='no_video' if module=='video' else 'complete')
            self.put('product_current_sections',product_id=3,module=module,section='product_info' if module=='basic_info' else 'sku_details' if module=='skus' else module,batch_id=index)
        completed=self.catalog(state='complete')['products'][0]
        self.assertEqual((completed['platform_product_id'],completed['valid_count'],completed['remaining_count']),('789',9,0))
        self.product(4,'888')
        self.put('collection_runs',id=40,product_id=4,status='failed',started_at=self.f.now)
        self.assertEqual(self.catalog(state='attempt')['products'][0]['platform_product_id'],'888')
        self.product(5,'777')
        self.put('list_observations',id=50,batch_id=10,product_id=5,slot_index=5)
        self.assertEqual(self.catalog(state='list')['products'][0]['platform_product_id'],'777')

    def test_sorting_pagination_and_bad_filters(self):
        """20条分页稳定、未知数量置后、区间非法返回明确错误。"""
        l=self.f.store.tables['list_observations']
        with self.f.engine.begin() as c:
            c.execute(l.update().where(l.c.id==11).values(sales_count=1000))
        for identifier in range(3,27): self.product(identifier,str(1000+identifier))
        first=self.catalog(sort_by='sales',order='desc')
        second=self.catalog(sort_by='sales',order='desc',page=2)
        self.assertEqual((first['total'],len(first['products']),len(second['products'])),(26,20,6))
        self.assertEqual(first['products'][0]['platform_product_id'],'123')
        self.assertFalse({row['id'] for row in first['products']} & {row['id'] for row in second['products']})
        self.assertEqual(self.catalog(sort_by='sales',order='asc')['products'][0]['platform_product_id'],'123')
        for suffix in ['sales_min=-1','sales_min=5&sales_max=1','sales_max=1.5','origin=invalid','state=invalid',
                       'since=bad','since=2026-10-26&until=2026-10-25','page=0','sort=invalid','order=invalid']:
            self.assertEqual(self.f.client.get('/collections/details?'+suffix).status_code,400,suffix)

    def test_thumbnail_falls_back_to_same_latest_list_when_detail_file_missing(self):
        """失效的详情缩略图回退最近列表同位置图片，不取他商品或未发布图片。"""
        data=b'isolated-image'
        target=self.f.settings.workspace/'output/catalog/list.jpg'
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        for identifier,path in [(1,'output/catalog/missing.jpg'),(2,'output/catalog/list.jpg')]:
            self.put('file_assets',id=identifier,root_key='workspace',relative_path=path,availability='present',size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        self.f.batch(30,'main_images',product_id=1)
        self.put('media_references',id=1,batch_id=30,role='main_image',position=1,file_asset_id=1,download_status='downloaded')
        self.put('media_references',id=2,batch_id=11,role='list_image',position=2,file_asset_id=2,download_status='downloaded')
        row=self.catalog(keyword='123')['products'][0]
        self.assertEqual(row['thumbnail'],2)
        self.assertNotIn('thumbnail_candidates',row)

    def test_old_routes_render_catalog_and_search_links_keep_product_boundary(self):
        """旧入口显示商品库，搜索词链接只查看当前商品，所有输出转义。"""
        for path in ['/collections','/collections/details','/collections?view=products','/collections?view=details']:
            response=self.f.client.get(path)
            self.assertEqual(response.status_code,200,path)
            body=response.get_data(as_text=True)
            self.assertIn('<h1>商品库</h1>',body)
            self.assertIn('data-catalog-product="456"',body)
            self.assertIn('product=123',body)
            self.assertIn('mode=records',body)
            self.assertNotIn('<script>商品</script>',body)
        self.assertIn('搜索词分类',self.f.client.get('/collections/searches').get_data(as_text=True))
