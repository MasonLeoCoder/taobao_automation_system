"""视频批次来源、主视频筛选及本地附件下载边界。"""
from datetime import datetime
import hashlib
import unittest
from tests.unit import test_collection_browser as fixtures
from ecommerce.modules.collection.queries import query


class VideoPageTests(unittest.TestCase):
    def setUp(self):
        """建立两个视频批次和本地原件；参数：无；返回：无。"""
        c=self.case=fixtures.CollectionBrowserTests();c.setUp();self.addCleanup(c.doCleanups)
        c.put('tasks',id='b'*32,name='collection.taobao.video',status='succeeded',started_at=c.now)
        for identifier,time in [(40,datetime(2026,10,24)),(50,c.now)]:
            c.batch(identifier,'video',product_id=1,status='downloaded',captured_at=time,task_id='b'*32 if identifier==40 else c.task)
            self.asset(identifier)
            c.put('media_references',id=identifier,batch_id=identifier,role='main_video',position=1,file_asset_id=identifier)
        c.put('product_current_sections',product_id=1,section='video',module='video',batch_id=50)
        self.asset(95,'.jpg')
        c.put('media_references',id=95,batch_id=50,role='main_image',position=2,file_asset_id=95)

    def asset(self,identifier,suffix='.mp4',relative=None):
        """生成脱敏文件和索引；参数：编号、后缀及可选路径；返回：路径。"""
        c=self.case;name=relative or f'output/video/sample-{identifier}{suffix}';path=c.settings.workspace/name
        path.parent.mkdir(parents=True,exist_ok=True);data=f'isolated-video-{identifier}'.encode();path.write_bytes(data)
        c.put('file_assets',id=identifier,root_key='workspace',relative_path=name,availability='present',size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        return path

    def test_historical_source_fixed_and_video_filter_hides_single_sort(self):
        """历史视频不借新批原件，封面或其他媒体不混入视频计数；参数：无；返回：断言。"""
        page=self.case.client.get('/collections/products/123?batch=40&kind=reviews').text
        self.assertIn('data-video-page',page);self.assertIn('当前批次 40',page)
        self.assertIn('src="/collections/files/40"',page);self.assertNotIn('src="/collections/files/50"',page)
        self.assertIn('/tasks/'+('b'*32),page);self.assertNotIn('data-video-sort',page)
        current=self.case.client.get('/collections/products/123?module=video').text
        self.assertIn('当前批次 50',current);self.assertIn('共 1 个',current)
        self.assertEqual(current.count('data-video-item='),1);self.assertNotIn('/collections/files/95',current)
        self.assertIn('preload="metadata"',current);self.assertNotIn(' autoplay',current)

    def test_multi_video_sort_and_page_two_single_row_still_has_sort(self):
        """正倒序按原顺序，排序显隐取整批数量而非当前页数量；参数：无；返回：断言。"""
        c=self.case
        for identifier in range(51,71):c.put('media_references',id=identifier,batch_id=50,role='main_video',position=identifier-49,file_asset_id=50)
        page=c.client.get('/collections/products/123?batch=50&page=2').text
        self.assertIn('共 21 个',page);self.assertIn('data-video-sort',page)
        self.assertEqual(page.count('data-video-item='),1)
        desc=query(c.settings,'records',batch_id=50,page=1,kind='media',main_video_only=True,order='desc')
        self.assertEqual(desc['records'][0]['position'],21);self.assertEqual(desc['total'],21)

    def test_download_attachment_range_head_and_existing_preview(self):
        """附件与预览共用校验，下载保持原文件名且支持Range和HEAD；参数：无；返回：断言。"""
        c=self.case
        with c.client.get('/collections/files/40?download=1',buffered=True) as response:
            self.assertEqual(response.status_code,200);self.assertEqual(response.data,b'isolated-video-40')
            self.assertIn('attachment;',response.headers['Content-Disposition']);self.assertIn('sample-40.mp4',response.headers['Content-Disposition'])
        with c.client.get('/collections/files/40?download=1',headers={'Range':'bytes=0-4'},buffered=True) as response:
            self.assertEqual(response.status_code,206);self.assertEqual(response.data,b'isola')
        with c.client.head('/collections/files/40?download=1') as response:
            self.assertEqual(response.status_code,200);self.assertEqual(response.data,b'')
        with c.client.get('/collections/files/40',buffered=True) as response:self.assertIn('inline;',response.headers['Content-Disposition'])
        self.assertEqual(c.client.get('/collections/files/40?download=bad').status_code,400)

    def test_missing_changed_unpublished_and_non_video_files_are_not_downloadable(self):
        """缺失/变更/未发布/非主视频/越界文件不能下载，旧记录仍保留；参数：无；返回：断言。"""
        c=self.case;path=c.settings.workspace/'output/video/sample-50.mp4';path.write_bytes(b'changed')
        self.assertEqual(c.client.get('/collections/files/50?download=1').status_code,409)
        page=c.client.get('/collections/products/123?batch=50').text
        self.assertIn('文件校验未通过',page);self.assertNotIn('data-video-download',page)
        path.unlink();self.assertEqual(c.client.get('/collections/files/50?download=1').status_code,404)
        self.assertIn('本地文件缺失',c.client.get('/collections/products/123?batch=50').text)
        self.assertEqual(c.client.get('/collections/files/95?download=1').status_code,404)
        c.put('media_references',id=96,batch_id=40,role='main_video',file_asset_id=95)
        self.assertEqual(c.client.get('/collections/files/95?download=1').status_code,404)
        self.assertNotIn('/collections/files/95',c.client.get('/collections/products/123?batch=40').text)
        c.batch(70,'reviews',product_id=1);self.asset(70)
        c.put('media_references',id=70,batch_id=70,role='review_video',file_asset_id=70)
        self.assertEqual(c.client.get('/collections/files/70?download=1').status_code,404)
        c.batch(80,'video',product_id=1,visibility='staging');self.asset(80)
        c.put('media_references',id=80,batch_id=80,role='main_video',file_asset_id=80)
        self.assertEqual(c.client.get('/collections/files/80?download=1').status_code,404)
        self.asset(81,relative='../outside.mp4');c.put('media_references',id=81,batch_id=40,role='main_video',file_asset_id=81)
        self.assertEqual(c.client.get('/collections/files/81?download=1').status_code,404)
        c.batch(60,'video',product_id=1,status='no_video')
        self.assertIn('本批次已确认无视频',c.client.get('/collections/products/123?batch=60').text)
