"""本机浏览器验证文件夹入口和反馈操作，千牛执行使用脱敏替身。"""
import os
import logging
import re
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from zipfile import ZipFile
from contextlib import ExitStack

from imageio_ffmpeg import get_ffmpeg_exe
from openpyxl import load_workbook
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
from ecommerce.web.app import create_app
from tests.unit.test_web_listing import bind_listing_fixture, fixture_materials, fixture_settings


@skipUnless(os.environ.get('QIANNIU_BROWSER_TESTS') == '1', '显式启用本地浏览器验证')
class WebListingBrowserTests(TestCase):
    def test_download_bundle_and_recognize_complete_folder_before_preflight(self):
        with TemporaryDirectory() as root, sync_playwright() as pw:
            root = Path(root)
            app = create_app(fixture_settings(root))
            server = make_server('127.0.0.1', 0, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={'width': 390, 'height': 840})
                page.goto(f'http://127.0.0.1:{server.server_port}/plans/listing')
                with page.expect_download() as downloaded:
                    page.get_by_role('link', name='下载粘毛器资料包').click()
                self.assertEqual(downloaded.value.suggested_filename, '粘毛器-商品资料包.zip')
                with ZipFile(downloaded.value.path()) as archive:
                    archive.extractall(root)
                folder = root / '粘毛器-商品资料包'
                book_path = folder / '产品信息.xlsx'
                book = load_workbook(book_path)
                try:
                    for row in (2, 5, 6):
                        for cell in book['填写示例'][row]:
                            book['Sheet1'][cell.coordinate] = cell.value
                    book.save(book_path)
                finally:
                    book.close()
                image = Image.new('RGB', (24, 24), 'white')
                for group, names in {
                    '1-1主图': ('1','2','3','4','5'), '3-4主图': ('1','2','3','4','5'),
                    '详情图': ('2','10'), 'sku图': ('短柄款',),
                }.items():
                    for name in names:
                        image.save(folder / group / f'{name}.png')
                video = folder / '商品视频.mp4'
                subprocess.run([get_ffmpeg_exe(), '-nostdin', '-v', 'error', '-f', 'lavfi',
                    '-i', 'color=c=white:s=32x32', '-t', '5', '-c:v', 'libx264', str(video)],
                    check=True, capture_output=True, timeout=15)

                picker = page.locator('#listing-folder')
                with patch('ecommerce.modules.product.web_listing.open_publish') as publish:
                    picker.set_input_files(str(folder))
                    summary = page.locator('#listing-recognized')
                    for text in ('产品信息.xlsx', '5 张 · 1-1主图', '5 张 · 3-4主图',
                                 '2 张 · 详情图', '1 张 · sku图', '商品视频.mp4'):
                        expect(summary).to_contain_text(text)
                    expect(page.locator('#listing-mapping')).to_be_hidden()
                    expect(page.locator('#listing-import-note')).to_contain_text('自检通过')
                    preview = page.get_by_role('link', name='预览待上传视频')
                    expect(preview).to_be_visible()
                    filename = page.locator('#listing-video-filename')
                    expect(filename).to_have_text('商品视频.mp4')
                    mobile_button, mobile_name = preview.bounding_box(), filename.bounding_box()
                    self.assertAlmostEqual(mobile_button['x'], mobile_name['x'], delta=1)
                    self.assertGreater(mobile_button['y'], mobile_name['y'] + mobile_name['height'])
                    page.set_viewport_size({'width': 1280, 'height': 900})
                    wide_button, wide_name = preview.bounding_box(), filename.bounding_box()
                    self.assertGreaterEqual(wide_button['x'], wide_name['x'] + wide_name['width'])
                    self.assertLess(wide_button['y'], wide_name['y'] + wide_name['height'])
                    color = preview.evaluate('link => getComputedStyle(link).backgroundColor')
                    preview.hover()
                    expect(preview).not_to_have_css('background-color', color)
                    with page.expect_popup() as opened:
                        preview.press('Enter')
                    player = opened.value
                    expect(player.locator('video')).to_have_js_property('duration', 5)
                    player.close()
                    page.set_viewport_size({'width': 390, 'height': 840})
                    expect(page.locator('#listing-issues')).to_contain_text('SKU 图 / 长柄款')

                    shutil.copyfile(video, folder / '备选视频.mp4')
                    picker.set_input_files(str(folder))
                    expect(summary).to_contain_text('有多个候选，请选择')
                    expect(page.locator('#listing-import-note')).to_contain_text('存在多个候选项')
                    page.locator('[data-material="video"] .ss-trigger').click()
                    page.get_by_role('option', name='粘毛器-商品资料包/商品视频.mp4', exact=True).click()
                    expect(summary).to_contain_text('商品视频.mp4')
                    page.get_by_role('button', name='导入并自检').click()
                    expect(page.locator('#listing-import-note')).to_contain_text('自检通过')
                    (folder / '备选视频.mp4').unlink()
                    video.write_bytes(b'invalid video')
                    picker.set_input_files(str(folder))
                    expect(page.locator('#listing-issues')).to_contain_text('视频无法读取或解码')
                    page.get_by_role('button', name='跳过视频并重新自检').click()
                    expect(page.locator('#listing-import-note')).to_contain_text('自检通过')
                    expect(preview).to_have_count(0)
                    expect(page.locator('#listing-offline-note')).to_have_text('请在启用 MySQL 的环境中选择已登记的千牛账号')
                    expect(page.get_by_role('button', name='仅填写', exact=True)).to_be_disabled()
                    expect(page.locator('#listing-submit')).to_be_disabled()
                    publish.assert_not_called()
                self.assertEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                browser.close()
            finally:
                server.shutdown()
                thread.join(5)

    def test_folder_preflight_only_fill_and_reload_during_wait(self):
        with TemporaryDirectory() as root, sync_playwright() as pw, ExitStack() as stack:
            folder = fixture_materials(Path(root))
            app = create_app(fixture_settings(Path(root)))
            stack.enter_context(bind_listing_fixture(app))
            server = make_server('127.0.0.1', 0, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                browser = pw.chromium.launch()
                try:
                    page = browser.new_page(viewport={'width': 390, 'height': 840})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    confirmed = threading.Event()
                    advanced = threading.Event()
                    closed = threading.Event()
                    verification_page = SimpleNamespace(is_closed=closed.is_set,
                        wait_for_timeout=lambda ms: time.sleep(ms/1000), frames=[], screenshot=lambda **_: None)

                    def fake_open(*args, **kwargs):
                        assert kwargs['submit'] is False
                        review = kwargs['web_review']
                        review.verification(SimpleNamespace(page=verification_page, step='商品编辑页验证'))
                        if not confirmed.is_set():
                            review.verification(SimpleNamespace(page=verification_page, step='验证尚未完成'))
                        assert confirmed.is_set()
                        advanced.set()
                        review.progress('千牛已登录，正在进入商品发布')
                        logging.getLogger('ecommerce.platforms.qianniu.product_video').warning('模拟视频素材需要人工处理')
                        review.attention('【需要人工处理弹窗】\n当前步骤：等待视频窗口关闭\n完成后输入“继续”。')
                        deadline = time.time() + 30
                        assert review.sku_image_review(verification_page, '灰色款 SKU 搜索主图未确认', deadline) == 'skip_image'
                        assert review.sku_image_review(verification_page, '蓝色款商品规格图片未确认', time.time() + 3) == 'timeout'
                        review.update(outcome='filled')
                        review._wait('已填写，检查后结束', ['finish'], kind='finish')
                        return '仅填写流程完成'

                    with patch('ecommerce.modules.product.web_listing.open_publish', side_effect=fake_open) as publish:
                        page.goto(f'http://127.0.0.1:{server.server_port}/plans/listing')
                        page.get_by_role('button', name=re.compile('^目标店铺，')).click()
                        page.get_by_role('option', name='测试店铺 · 123', exact=True).click()
                        page.get_by_role('button', name=re.compile('^千牛账号，')).click()
                        page.get_by_role('option', name='tb123:测试', exact=True).click()
                        page.locator('#listing-folder').set_input_files(str(folder))
                        expect(page.locator('#listing-import-note')).to_contain_text('自检通过', timeout=10000)
                        expect(page.locator('#listing-submit')).to_be_checked()
                        expect(page.get_by_text('DEMO-1-A', exact=True)).to_be_visible()
                        self.assertEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                        sku = page.locator('[data-sku-key="sku-1"]')
                        guide = page.locator('#listing-guide')
                        start = page.locator('#listing-start')
                        original_guide = guide.input_value()
                        for value, valid in (('小熊款猫包黄色双肩', False), ('猫' * 10, True),
                                             ('猫' * 15, True), ('猫' * 15 + 'A', False)):
                            sku.fill(value)
                            expect(sku).to_have_attribute('aria-invalid', 'false' if valid else 'true')
                            if valid:
                                expect(start).to_be_enabled()
                            else:
                                expect(start).to_be_disabled()
                        sku.fill('')
                        guide.fill('小熊款猫包 黄 色双肩')
                        expect(start).to_be_disabled()
                        expect(page.locator('#listing-sku-1-fallback')).to_contain_text('18 / 30')
                        sku.fill('小熊款猫包黄色双肩款')
                        expect(start).to_be_enabled()
                        sku.fill('')
                        guide.fill('')
                        expect(start).to_be_enabled()
                        expect(page.locator('#listing-sku-1-fallback')).to_contain_text('将跳过')
                        guide.fill(original_guide)
                        page.locator('#listing-submit').uncheck()
                        page.get_by_role('button', name='仅填写', exact=True).click()
                        dialog = page.get_by_role('dialog', name='需要人工处理')
                        expect(dialog).to_be_visible(timeout=10000)
                        dialog.get_by_role('button', name='完成', exact=True).click()
                        expect(dialog).to_contain_text('验证尚未完成', timeout=10000)
                        confirmed.set()
                        dialog.get_by_role('button', name='关闭人工处理提示').click()
                        self.assertFalse(advanced.wait(.4))
                        expect(page.locator('#listing-phase')).to_have_text('等待人工处理')
                        page.reload()
                        expect(dialog).to_be_visible(timeout=10000)
                        self.assertFalse(advanced.is_set())
                        dialog.get_by_role('button', name='完成', exact=True).click()
                        video = page.get_by_role('dialog', name='需要人工处理')
                        expect(video).to_contain_text('等待视频窗口关闭', timeout=10000)
                        expect(video).to_contain_text('点击「完成」')
                        expect(page.locator('#listing-log-rows')).to_contain_text('模拟视频素材需要人工处理', timeout=10000)
                        video.get_by_role('button', name='完成', exact=True).click()
                        image = page.get_by_role('dialog', name='请处理 SKU 图片')
                        expect(image).to_contain_text('灰色款 SKU 搜索主图', timeout=10000)
                        expect(image.get_by_role('timer')).to_contain_text('到时自动跳过')
                        before_reload = image.get_by_role('timer').inner_text()
                        page.reload()
                        expect(image).to_contain_text('灰色款 SKU 搜索主图', timeout=10000)
                        after_reload = image.get_by_role('timer').inner_text()
                        self.assertLessEqual(after_reload, before_reload)
                        image.get_by_role('button', name='跳过此图片').click()
                        expect(image).to_contain_text('蓝色款商品规格图片', timeout=10000)
                        page.get_by_role('button', name='结束检查').click(timeout=10000)
                        expect(page.locator('#listing-message')).to_contain_text('仅填写流程完成', timeout=10000)
                        expect(page.locator('#listing-log-rows')).to_contain_text('仅填写流程完成', timeout=10000)
                        publish.assert_called_once()
                    self.assertEqual(errors, [])
                finally:
                    closed.set()
                    browser.close()
            finally:
                server.shutdown()
                thread.join(5)
