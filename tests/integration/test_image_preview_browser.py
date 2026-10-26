"""全站图片本页预览：隔离服务与图片，禁止真实平台请求和采集动作。"""
import hashlib
import os
import struct
import unittest
import zlib

from playwright.sync_api import expect
from tests.integration import test_web_collection_browser as fixtures


def png(width,height):
    """创建合成PNG；参数：尺寸；返回：可解码图片字节，不依赖额外图库。"""
    def chunk(kind,data):
        """编码PNG数据段；参数：类型及数据；返回：含长度和CRC的字节。"""
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    pixels=(b'\0'+bytes((120,175,155))*width)*height
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS')=='1','显式启用隔离图片预览检查')
class ImagePreviewBrowserTests(unittest.TestCase):
    def setUp(self):
        """复用隔离服务，加入长图、小图和宽图；参数：无；返回：无。"""
        self.host=fixtures.WebCollectionBrowserTests();self.host.setUp();self.addCleanup(self.host.doCleanups)
        self.fixture=self.host.fixture;self.page=self.host.page;self.context=self.host.context
        for identifier,width,height in [(501,1200,1600),(502,80,60),(503,2400,360)]:
            path=self.host.settings.workspace/'output/preview'/f'{identifier}.png'
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(png(width,height))
            self.fixture.put('file_assets',id=identifier,root_key='workspace',relative_path=path.relative_to(self.host.settings.workspace).as_posix(),
                availability='present',size_bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.fixture.batch(30,'main_images',product_id=1)
        self.fixture.put('product_current_sections',product_id=1,section='main_images',module='main_images',batch_id=30)
        self.fixture.put('media_references',id=1001,batch_id=30,role='main_image',position=1,file_asset_id=501,download_status='downloaded')
        self.fixture.put('media_references',id=1004,batch_id=30,role='main_image',position=2,file_asset_id=503,download_status='downloaded')
        self.fixture.put('media_references',id=1002,batch_id=11,role='list_image',position=2,file_asset_id=501,download_status='downloaded')
        self.fixture.batch(70,'skus',product_id=1)
        self.fixture.put('product_current_sections',product_id=1,section='sku_details',module='skus',batch_id=70)
        self.fixture.put('sku_observations',id=700,batch_id=70,position=1,sku_name='小图规格',price_value=1,image_status='downloaded')
        self.fixture.put('media_references',id=1003,batch_id=70,role='sku_image',position=1,sku_observation_id=700,
            file_asset_id=502,download_status='downloaded')
        self.viewer=self.page.locator('#image-preview');self.photo=self.page.locator('#image-preview-photo')

    def goto(self,path):
        """打开临时页面并等待公共脚本；参数：站内路径；返回：无。"""
        self.page.goto('http://127.0.0.1'+path)
        self.page.wait_for_function("document.querySelector('main img[data-image-preview-ready]') !== null")

    def test_all_existing_image_entries_open_here_and_close_on_image(self):
        """商品、搜索汇总、采集列表、批次、素材和SKU均复用本页预览。"""
        paths=['/collections/details','/collections/searches/products?category=粘毛器',
            '/collections/searches/'+self.fixture.task+'?batch=11','/collections/batches/11',
            '/collections/products/123?module=main_images','/collections/batches/30',
            '/collections/products/123?module=skus','/collections/batches/70']
        for path in paths:
            with self.subTest(path=path):
                self.goto(path);url=self.page.url;thumb=self.page.locator('main img').first
                expected=thumb.get_attribute('src');thumb.click()
                expect(self.viewer).to_be_visible();expect(self.photo).to_be_visible()
                self.assertTrue(self.photo.get_attribute('src').endswith(expected))
                self.assertEqual(len(self.context.pages),1);self.assertEqual(self.page.url,url)
                self.photo.click();expect(self.viewer).not_to_be_visible()
        self.assertEqual(self.host.errors,[])

    def test_each_close_path_restores_scroll_focus_and_query(self):
        """重复关闭不丢失滚动、页码、排序或焦点，预览期间锁定背景。"""
        for i in range(2,26):
            self.fixture.put('sku_observations',id=700+i,batch_id=70,position=i,sku_name=f'规格{i}',price_value=i,image_status='downloaded')
            self.fixture.put('media_references',id=1100+i,batch_id=70,role='sku_image',position=i,
                sku_observation_id=700+i,file_asset_id=502,download_status='reused')
        self.goto('/collections/products/123?module=skus&page=2&sort=price&order=desc')
        thumb=self.page.locator('main img').first;opener=thumb.locator('..');url=self.page.url
        for method in ['image','blank','button','escape']:
            with self.subTest(method=method):
                thumb.scroll_into_view_if_needed();y=self.page.evaluate('window.scrollY');self.assertGreater(y,0)
                thumb.click();expect(self.photo).to_be_visible()
                self.assertEqual(self.page.evaluate('getComputedStyle(document.body).position'),'fixed')
                self.page.mouse.wheel(0,500)
                if method=='image':self.photo.click()
                elif method=='blank':self.page.mouse.click(2,2)
                elif method=='button':self.viewer.get_by_role('button',name='关闭图片预览').click()
                else:self.page.keyboard.press('Escape')
                expect(self.viewer).not_to_be_visible()
                self.assertAlmostEqual(self.page.evaluate('window.scrollY'),y,delta=1)
                self.assertEqual(self.page.url,url)
                self.assertTrue(opener.evaluate('e=>e===document.activeElement'))
                self.assertNotEqual(self.page.evaluate('getComputedStyle(document.body).position'),'fixed')

    def test_small_image_keyboard_and_original_entry(self):
        """小图不过度拉伸，键盘可开关；只有主动打开原图才新建标签。"""
        self.goto('/collections/products/123?module=skus');opener=self.page.locator('.sku-image-link')
        opener.focus();self.page.keyboard.press('Enter');expect(self.photo).to_be_visible()
        size=self.photo.evaluate('e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height})')
        self.assertEqual(size,{'width':80,'height':60})
        with self.page.expect_popup() as opened:self.viewer.get_by_role('link',name='打开原图').click()
        original=opened.value;original.wait_for_load_state();self.assertTrue(original.url.endswith('/collections/files/502'))
        original.close();expect(self.viewer).to_be_visible();self.page.keyboard.press('Escape')
        opener.focus();self.page.keyboard.press(' ');expect(self.photo).to_be_visible()
        self.page.keyboard.press('Escape');self.assertTrue(opener.evaluate('e=>e===document.activeElement'))

    def test_failed_image_is_explained_and_next_open_recovers(self):
        """加载失败显示原因，关闭后正常图片仍可预览。"""
        self.goto('/collections/products/123?module=main_images');thumb=self.page.locator('main img').first
        thumb.evaluate("e=>e.dataset.previewSrc='/collections/files/999999'")
        thumb.click();expect(self.viewer).to_be_visible()
        expect(self.page.locator('#image-preview-status')).to_contain_text('图片加载失败')
        expect(self.photo).not_to_be_visible();self.page.keyboard.press('Escape')
        thumb.evaluate('e=>delete e.dataset.previewSrc');thumb.click();expect(self.photo).to_be_visible()
        expect(self.page.locator('#image-preview-status')).not_to_be_visible()
        box=self.photo.bounding_box();self.assertLessEqual(box['height'],self.page.viewport_size['height'])
        self.assertAlmostEqual(box['width']/box['height'],1200/1600,places=2)
        self.photo.click()

    def test_dynamic_image_is_supported_and_manual_prompt_has_priority(self):
        """动态图片可键盘打开，人工采集弹窗出现时先退出预览。"""
        self.goto('/collections/products/123?module=skus')
        self.page.evaluate("""() => {const image=document.createElement('img');image.src='/collections/files/503';image.alt='动态宽图';document.querySelector('main').append(image)}""")
        thumb=self.page.get_by_role('button',name='放大查看：动态宽图');expect(thumb).to_be_visible()
        thumb.focus();self.page.keyboard.press(' ');expect(self.photo).to_be_visible()
        box=self.photo.bounding_box();self.assertLessEqual(box['width'],self.page.viewport_size['width'])
        self.assertAlmostEqual(box['width']/box['height'],2400/360,places=2)
        self.page.evaluate("document.getElementById('collection-attention').showModal()")
        expect(self.viewer).not_to_be_visible();expect(self.page.locator('#collection-attention')).to_be_visible()
        self.assertNotEqual(self.page.evaluate('getComputedStyle(document.body).position'),'fixed')

    def test_product_title_still_navigates_instead_of_previewing(self):
        """商品图与标题操作各自清晰，点击标题照常进入资料页。"""
        self.goto('/collections/details')
        self.page.locator('.product-cell').first.locator('a').last.click()
        self.assertIn('/collections/products/123',self.page.url)
        expect(self.viewer).not_to_be_visible()


if __name__=='__main__':unittest.main()
