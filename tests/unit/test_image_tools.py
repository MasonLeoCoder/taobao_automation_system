"""图片转换及工具箱的真实编码、来源隔离与异常下载检查。"""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile
import hashlib
import unittest

from PIL import Image
from ecommerce.infrastructure.images.converter import convert_image
from ecommerce.infrastructure.images.models import ImageConversionError
from ecommerce.modules.image_tools.service import convert_uploads
from tests.unit import test_collection_browser as fixtures


def picture(fmt='PNG', *, size=(8, 5), color=(255, 0, 0, 0), orientation=None):
    """生成可解码的脱敏图片；参数：格式、尺寸、颜色和方向；返回：字节。"""
    result=BytesIO()
    with Image.new('RGBA',size,color) as image:
        if fmt=='JPEG':
            image=image.convert('RGB')
        options={}
        if orientation:
            exif=Image.Exif();exif[274]=orientation;options['exif']=exif
        image.save(result,fmt,**options)
    return result.getvalue()


class ConverterTests(unittest.TestCase):
    def test_png_preserves_alpha_and_dimensions_and_jpg_composites_white(self):
        """转换输出真实编码，透明像素转JPG为白底；参数：无；返回：断言。"""
        source=picture('WEBP')
        png=convert_image(source,'png');jpg=convert_image(source,'jpg')
        with Image.open(BytesIO(png.data)) as image:
            self.assertEqual((image.format,image.size),('PNG',(8,5)));self.assertEqual(image.getpixel((0,0))[3],0)
        with Image.open(BytesIO(jpg.data)) as image:
            self.assertEqual((image.format,image.size),('JPEG',(8,5)));self.assertTrue(all(v>=250 for v in image.getpixel((0,0))))

    def test_original_and_same_format_reuse_bytes(self):
        """保持原格式及无需处理的同格式逐字节保留；参数：无；返回：断言。"""
        for fmt,target in [('PNG','png'),('JPEG','jpg'),('WEBP','original')]:
            source=picture(fmt)
            self.assertEqual(convert_image(source,target).data,source)
            self.assertEqual(convert_image(source,'original').data,source)

    def test_orientation_corrected_except_original(self):
        """旋转信息只在生成转换副本时应用且不再二次旋转；参数：无；返回：断言。"""
        source=picture('JPEG',orientation=6)
        self.assertEqual(convert_image(source,'original').data,source)
        for target in ['png','jpg']:
            result=convert_image(source,target)
            with Image.open(BytesIO(result.data)) as image:
                self.assertEqual(image.size,(5,8));self.assertIn(image.getexif().get(274,1),(1,None))

    def test_corrupt_animated_unsupported_and_limits(self):
        """坏图、动图、非支持格式和资源上限均明确拒绝；参数：无；返回：断言。"""
        for source in [b'bad',picture()[:40],picture('BMP')]:
            with self.assertRaises(ImageConversionError):convert_image(source)
        buffer=BytesIO()
        with Image.new('RGB',(2,2),'red') as first,Image.new('RGB',(2,2),'blue') as second:
            first.save(buffer,'GIF',save_all=True,append_images=[second])
        with self.assertRaisesRegex(ImageConversionError,'动图'):convert_image(buffer.getvalue())
        with patch('ecommerce.infrastructure.images.converter.MAX_PIXELS',1):
            with self.assertRaisesRegex(ImageConversionError,'像素'):convert_image(picture())
        with patch('ecommerce.infrastructure.images.converter.MAX_FILE_BYTES',1):
            with self.assertRaisesRegex(ImageConversionError,'10 MB'):convert_image(picture())
        with self.assertRaises(ImageConversionError):convert_image(picture(),'gif')

    def test_zip_names_collision_cleanup_and_no_partial_result(self):
        """同名图片安全去重，失败不返回残缺包且关闭临时文件；参数：无；返回：断言。"""
        with TemporaryDirectory() as tmp:
            artifact=convert_uploads([('../../A.webp',BytesIO(picture('WEBP'))),('a.jpg',BytesIO(picture('JPEG')))],'png',temp_dir=Path(tmp))
            with artifact.stream,ZipFile(artifact.stream) as zipped:
                self.assertEqual(zipped.namelist(),['A.png','a_2.png'])
                for name in zipped.namelist():
                    with Image.open(BytesIO(zipped.read(name))) as image:self.assertEqual(image.format,'PNG')
            with self.assertRaisesRegex(ImageConversionError,'broken'):
                convert_uploads([('ok.png',BytesIO(picture())),('broken.png',BytesIO(b'bad'))],'png',temp_dir=Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()),[])
            with self.assertRaisesRegex(ImageConversionError,'1 至 20'):convert_uploads([],'png',temp_dir=Path(tmp))


class ImageToolPageTests(unittest.TestCase):
    def setUp(self):
        """使用独立数据库及合成图片，不接触正式业务；参数：无；返回：无。"""
        self.case=fixtures.CollectionBrowserTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        c=self.case
        target=patch('ecommerce.infrastructure.storage.main_image_export.collection_store',return_value=c.store)
        target.start();self.addCleanup(target.stop)
        self.headers={'X-Collection-Token':c.app.config['COLLECTION_TOKEN'],'Origin':'http://localhost'}
        for batch in [40,50]:
            c.batch(batch,'main_images',product_id=1)
            for number in range(1,4):self.asset(batch,batch*10+number,number)
        c.put('product_current_sections',product_id=1,section='main_images',module='main_images',batch_id=50)

    def asset(self,batch,identifier,position,*,role='main_image',relative=None):
        """插入本测试图片原件及索引；参数：批次、编号、位置和覆盖项；返回：原件路径。"""
        c=self.case;relative=relative or f'output/images/{identifier}.webp';path=c.settings.workspace/relative
        path.parent.mkdir(parents=True,exist_ok=True);data=picture('WEBP',color=(identifier%255,80,90,255));path.write_bytes(data)
        c.put('file_assets',id=identifier,root_key='workspace',relative_path=relative,availability='present',size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        c.put('media_references',id=identifier,batch_id=batch,position=position,role=role,file_asset_id=identifier)
        return path

    def post(self,body,product='123',batch=50):
        """发起有令牌主图导出；参数：正文和来源；返回：测试响应。"""
        return self.case.client.post(f'/api/tools/images/products/{product}/batches/{batch}/download',json=body,headers=self.headers,buffered=True)

    def test_tool_pages_navigation_and_uploaded_real_attachment(self):
        """工具入口可达且上传返回真实PNG附件；参数：无；返回：断言。"""
        c=self.case
        self.assertIn('图片格式转换',c.client.get('/tools').text)
        self.assertIn('data-image-converter',c.client.get('/tools/images').text)
        self.assertIn('工具箱',c.client.get('/').text)
        with c.client.post('/api/tools/images/convert',data={'format':'png','files':(BytesIO(picture('WEBP')),'sample.webp')},headers=self.headers,buffered=True) as response:
            self.assertEqual(response.status_code,200);self.assertEqual(response.mimetype,'image/png')
            self.assertIn('sample.png',response.headers['Content-Disposition'])
            with Image.open(BytesIO(response.data)) as image:self.assertEqual(image.format,'PNG')

    def test_token_origin_host_and_payload_rejections(self):
        """其他来源、错令牌和非法输入均拒绝；参数：无；返回：断言。"""
        c=self.case;url='/api/tools/images/convert'
        self.assertEqual(c.client.post(url).status_code,403)
        self.assertEqual(c.client.post(url,headers={**self.headers,'Origin':'https://other.example'}).status_code,403)
        self.assertEqual(c.client.get('/tools',base_url='http://other.example').status_code,403)
        self.assertEqual(c.client.post(url,json={},headers=self.headers).status_code,415)
        for body in [{},{'format':[]},{'format':'gif'},{'format':'png','media_id':True},{'format':'png','path':'secret'}]:
            self.assertEqual(self.post(body).status_code,400)
        self.assertEqual(c.app.config['MAX_CONTENT_LENGTH'],1024*1024)

    def test_main_images_fixed_batch_zip_all_pages_and_single(self):
        """批量下载全批主图而非当前页，单张按媒体编号定位且原件不变；参数：无；返回：断言。"""
        c=self.case;self.asset(50,599,99,role='detail_image')
        before={path:path.read_bytes() for path in (c.settings.workspace/'output/images').iterdir()}
        with self.post({'format':'png'},batch=40) as response:
            self.assertEqual(response.status_code,200)
            with ZipFile(BytesIO(response.data)) as archive:
                self.assertEqual(len(archive.namelist()),3);self.assertTrue(all('_批次40_' in name for name in archive.namelist()))
        with self.post({'format':'jpg','media_id':501}) as response:
            self.assertEqual(response.status_code,200)
            with Image.open(BytesIO(response.data)) as image:self.assertEqual(image.format,'JPEG')
        self.assertEqual(self.post({'format':'png','media_id':401}).status_code,404)
        self.assertEqual(self.post({'format':'png'},product='456').status_code,404)
        for path,data in before.items():self.assertEqual(path.read_bytes(),data)
        for number in range(4,25):self.asset(50,600+number,number)
        with self.post({'format':'png'}) as response:
            with ZipFile(BytesIO(response.data)) as archive:self.assertEqual(len(archive.namelist()),24)

    def test_missing_changed_and_unpublished_fail_without_partial_archive(self):
        """缺失、哈希变化及未发布来源不产生残缺包；参数：无；返回：断言。"""
        c=self.case;path=c.settings.workspace/'output/images/502.webp';source=path.read_bytes();path.write_bytes(b'changed')
        response=self.post({'format':'png'});self.assertEqual(response.status_code,422);self.assertIn('校验',response.json['error'])
        path.unlink();self.assertEqual(self.post({'format':'original'}).status_code,422)
        self.assertIn('本地原件缺失',c.client.get('/collections/products/123?module=main_images').text)
        c.batch(70,'main_images',product_id=1,visibility='staging');self.asset(70,701,1)
        self.assertEqual(self.post({'format':'png'},batch=70).status_code,404)
        c.batch(80,'main_images',product_id=1);self.asset(80,801,1,relative='../outside.webp')
        self.assertEqual(self.post({'format':'png'},batch=80).status_code,422)

    def test_gallery_batch_binding_sort_and_non_main_role(self):
        """画廊只读当前加载批次，倒序保持编号，其他媒体不占主图数；参数：无；返回：断言。"""
        c=self.case;self.asset(50,599,99,role='detail_image')
        page=c.client.get('/collections/products/123?batch=50&kind=reviews&order=desc').text
        self.assertIn('data-main-image-page',page);self.assertIn('共 3 张',page)
        self.assertLess(page.index('data-main-image="503"'),page.index('data-main-image="501"'))
        self.assertNotIn('/collections/files/599',page);self.assertIn('主图 3',page)
        old=c.client.get('/collections/products/123?batch=40').text
        self.assertIn('当前批次 40',old);self.assertIn('/batches/40/download',old)
        self.assertNotIn('data-main-image="501"',old)

    def test_upload_over_one_megabyte_does_not_raise_global_limit(self):
        """图片专用限制允许大于1MB的合法原件，全站默认上限保持；参数：无；返回：断言。"""
        source=picture()+b'\0'*(1024*1024+5)
        with self.case.client.post('/api/tools/images/convert',data={'format':'original','files':(BytesIO(source),'large.png')},headers=self.headers,buffered=True) as response:
            self.assertEqual(response.status_code,200);self.assertEqual(response.data,source)
            # 测试客户端生成的整个multipart输入由测试持有；模拟服务器在请求结束时关闭。
            response.request.environ['wsgi.input'].close()
        self.assertEqual(self.case.app.config['MAX_CONTENT_LENGTH'],1024*1024)
