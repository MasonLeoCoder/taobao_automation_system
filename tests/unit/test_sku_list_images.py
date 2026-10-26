"""SKU行图片严格关联同批规格，分页排序不串图，缺失或变化不影响原记录。"""
import hashlib
import unittest
import sqlalchemy as sa

from ecommerce.modules.collection.queries import query
from tests.unit import test_collection_browser as fixtures


class SkuListImageTests(unittest.TestCase):
    def setUp(self):
        """创建隔离查询库和本地图片目录；参数：无；返回：无。"""
        self.fixture=fixtures.CollectionBrowserTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.settings=self.fixture.settings;self.client=self.fixture.client
        self.fixture.batch(70,'skus',product_id=1)
        self.fixture.put('product_current_sections',product_id=1,section='sku_details',module='skus',batch_id=70)

    def sku(self,index,**values):
        """添加合成规格；参数：顺序及覆盖字段；返回：观察编号。"""
        self.fixture.put('sku_observations',**{'id':200+index,'batch_id':70,'position':index,
            'sku_name':'规格'+str(index),'price_value':index,'image_status':'downloaded',**values})
        return 200+index

    def image(self,index,sku,**values):
        """添加独立图片并关联规格，不依赖图片顺序；参数：编号、规格及覆盖字段；返回：文件路径。"""
        path=self.settings.workspace/'output/sku-list-images'/f'{index}.png'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(('image-'+str(index)).encode())
        self.fixture.put('file_assets',id=300+index,root_key='workspace',relative_path=path.relative_to(self.settings.workspace).as_posix(),
            size_bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),availability='present')
        self.fixture.put('media_references',**{'id':300+index,'batch_id':70,'role':'sku_image',
            'sku_observation_id':sku,'position':1000-index,'download_status':'downloaded','file_asset_id':300+index,**values})
        return path

    def records(self,**options):
        """通过实际业务入口读取；参数：排序分页；返回：本页记录。"""
        return query(self.settings,'records',batch_id=70,page=options.pop('page',1),**options)['records']

    def test_exact_sku_identity_follows_sort_and_both_page_entries(self):
        one=self.sku(1);two=self.sku(2);self.image(1,two);self.image(2,one)
        rows=self.records(sort_by='price',order='desc')
        self.assertEqual([(r['id'],r['thumbnail']) for r in rows],[(two,301),(one,302)])
        for url in ['/collections/products/123?module=skus','/collections/batches/70']:
            body=self.client.get(url).get_data(as_text=True)
            self.assertIn('aria-label="查看规格图：规格1"',body);self.assertIn('/collections/files/302',body)
            self.assertNotIn('<figure>',body);self.assertNotIn('素材排序',body)
        response=self.client.get('/collections/files/302');self.assertEqual(response.status_code,200);response.close()

    def test_other_batch_and_position_only_references_are_never_borrowed(self):
        sku=self.sku(1);self.fixture.batch(71,'skus',product_id=1)
        self.image(1,sku,batch_id=71,position=1)
        self.image(2,None,position=1)
        self.assertIsNone(self.records()[0]['thumbnail'])

    def test_missing_changed_download_failed_and_no_image_are_distinct(self):
        missing=self.image(1,self.sku(1));changed=self.image(2,self.sku(2))
        self.sku(3,image_status='no_image');self.sku(4,image_status='download_failed')
        missing.unlink();changed.write_bytes(b'changed')
        rows=self.records()
        self.assertEqual([row['thumbnail_message'] for row in rows],
            ['本地图片缺失','图片校验失败','页面无规格图','图片下载失败'])
        self.assertTrue(all(row['thumbnail'] is None for row in rows))
        body=self.client.get('/collections/products/123?module=skus').get_data(as_text=True)
        for row in rows:self.assertIn(row['thumbnail_message'],body)
        self.assertEqual(self.client.get('/collections/files/301').status_code,404)
        self.assertEqual(self.client.get('/collections/files/302').status_code,409)
        with self.fixture.engine.connect() as c:
            table=self.fixture.store.tables['file_assets']
            self.assertEqual(list(c.execute(sa.select(table.c.availability)).scalars()),['present','present'])

    def test_one_pagination_controls_twenty_rows_and_images_after_sorting(self):
        for i in range(1,46):self.image(i,self.sku(i))
        rows=self.records(page=2,sort_by='price',order='desc')
        self.assertEqual(len(rows),20);self.assertEqual(rows[0]['position'],25)
        self.assertEqual([r['thumbnail'] for r in rows],[300+i for i in range(25,5,-1)])
        body=self.client.get('/collections/products/123?module=skus&page=2&sort=price&order=desc&media_page=999').get_data(as_text=True)
        self.assertEqual(body.count('class="sku-thumbnail"'),20)
        self.assertNotIn('media_page=',body);self.assertIn('page=3',body)
        last=self.records(page=3);self.assertEqual(len(last),5)


if __name__=='__main__':unittest.main()
