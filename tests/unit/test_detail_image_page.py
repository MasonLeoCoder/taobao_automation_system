"""详情图片的整批下载、批次隔离与原件保留，均使用隔离数据库。"""
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile
import unittest
from PIL import Image
from tests.unit import test_image_tools as fixtures
from ecommerce.infrastructure.storage.mysql import MySQLStorageError


class DetailImageTests(unittest.TestCase):
    def setUp(self):
        """准备独立主图与详情图；参数：无；返回：无。"""
        self.fixture=fixtures.ImageToolPageTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.c=self.fixture.case;self.headers=self.fixture.headers
        for batch in (90,91):
            self.c.batch(batch,'detail_images',product_id=1,task_id='b'*32 if batch==91 else self.c.task)
            for n in range(1,25 if batch==90 else 3):self.fixture.asset(batch,batch*100+n,n,role='detail_image')
        self.c.put('product_current_sections',product_id=1,section='detail_images',module='detail_images',batch_id=90)
        self.url='/api/tools/images/products/123/detail-batches/90/download'

    def test_all_pages_single_formats_and_original_integrity(self):
        """整批包含24张，单张编码和原格式正确，采集原件不变。"""
        before={p:p.read_bytes() for p in (self.c.settings.workspace/'output/images').iterdir()}
        with self.c.client.post(self.url,json={'format':'png'},headers=self.headers) as response:
            self.assertEqual(response.status_code,200)
            with ZipFile(BytesIO(response.data)) as archive:
                self.assertEqual(len(archive.namelist()),24)
                self.assertTrue(all('_批次90_详情图片' in name for name in archive.namelist()))
                with Image.open(BytesIO(archive.read(archive.namelist()[0]))) as image:self.assertEqual(image.format,'PNG')
        for fmt,expected in [('png','PNG'),('jpg','JPEG')]:
            with self.c.client.post(self.url,json={'format':fmt,'media_id':9001},headers=self.headers) as response:
                with Image.open(BytesIO(response.data)) as image:self.assertEqual(image.format,expected)
        with self.c.client.post(self.url,json={'format':'original','media_id':9001},headers=self.headers) as response:
            self.assertEqual(response.data,before[self.c.settings.workspace/'output/images/9001.webp'])
        for path,content in before.items():self.assertEqual(path.read_bytes(),content)

    def test_wrong_role_product_module_and_unpublished_are_rejected(self):
        """归属与类型双重限制，详情图和主图不能互相借用。"""
        self.fixture.asset(90,9999,99,role='main_image')
        self.assertEqual(self.c.client.post(self.url,json={'format':'png','media_id':9999},headers=self.headers).status_code,404)
        self.assertEqual(self.c.client.post(self.url.replace('/123/','/456/'),json={'format':'png'},headers=self.headers).status_code,404)
        self.assertEqual(self.c.client.post(self.url.replace('/90/','/50/'),json={'format':'png'},headers=self.headers).status_code,404)
        self.assertEqual(self.c.client.post(self.url.replace('/detail-batches/','/batches/'),json={'format':'png'},headers=self.headers).status_code,404)
        self.c.batch(92,'detail_images',product_id=1,visibility='staging')
        self.assertEqual(self.c.client.post(self.url.replace('/90/','/92/'),json={'format':'png'},headers=self.headers).status_code,404)
        self.assertEqual(self.c.client.post(self.url,json={'format':'png'}).status_code,403)

    def test_missing_or_changed_file_fails_entire_archive(self):
        """缺失或哈希变化不输出残缺包，原媒体记录仍显示。"""
        path=self.c.settings.workspace/'output/images/9002.webp';path.write_bytes(b'changed')
        response=self.c.client.post(self.url,json={'format':'png'},headers=self.headers)
        self.assertEqual(response.status_code,422);self.assertIn('未输出不完整',response.json['error'])
        path.unlink();self.assertEqual(self.c.client.post(self.url,json={'format':'original'},headers=self.headers).status_code,422)
        self.assertIn('本地原件缺失',self.c.client.get('/collections/products/123?module=detail_images').text)

    def test_history_count_roles_original_numbers_and_empty_states(self):
        """标题统计全批，倒序保留编号，历史时间/来源固定且无图片状态独立。"""
        self.fixture.asset(90,9999,99,role='main_image')
        page=self.c.client.get('/collections/products/123?module=detail_images&kind=reviews&order=desc').text
        self.assertIn('共 24 张',page);self.assertEqual(page.count('<article class="ui-image-card'),20)
        self.assertLess(page.index('data-detail-image="9024"'),page.index('data-detail-image="9023"'))
        self.assertNotIn('/collections/files/9999',page)
        old=self.c.client.get('/collections/products/123?batch=91&image_view=reading').text
        self.assertIn('当前批次 91',old);self.assertIn('/detail-batches/91/download',old)
        self.assertIn('data-image-task href="/tasks/'+('b'*32),old)
        self.assertIn('data-layout="reading"',old)
        self.c.batch(93,'detail_images',product_id=1,status='no_images')
        self.assertIn('本批已确认无详情图片',self.c.client.get('/collections/products/123?batch=93').text)
        with patch('ecommerce.web.routes.pages.collection_query',side_effect=MySQLStorageError('模拟断开')):
            error=self.c.client.get('/collections/products/123?module=detail_images').text
        self.assertIn('图片资料读取失败',error);self.assertNotIn('本批已确认无详情图片',error)
