"""本机浏览器验证文件夹入口和反馈操作，千牛执行使用脱敏替身。"""
import os
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
from ecommerce.web.app import create_app
from tests.unit.test_web_listing import fixture_materials, fixture_settings


@skipUnless(os.environ.get('QIANNIU_BROWSER_TESTS') == '1', '显式启用本地浏览器验证')
class WebListingBrowserTests(TestCase):
    def test_folder_preflight_only_fill_and_reload_during_wait(self):
        with TemporaryDirectory() as root, sync_playwright() as pw:
            folder = fixture_materials(Path(root))
            app = create_app(fixture_settings(Path(root)))
            server = make_server('127.0.0.1', 0, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                browser = pw.chromium.launch()
                try:
                    page = browser.new_page(viewport={'width': 390, 'height': 840})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    scanned = threading.Event()
                    advanced = threading.Event()
                    closed = threading.Event()
                    login_page = SimpleNamespace(is_closed=closed.is_set, wait_for_timeout=lambda ms: time.sleep(ms/1000))
                    login_page.context = SimpleNamespace(pages=[login_page])

                    def fake_open(*args, **kwargs):
                        assert kwargs['submit'] is False
                        review = kwargs['web_review']
                        review.login_wait(login_page)
                        if not scanned.is_set():
                            review.login_wait(login_page, retry=True)
                        assert scanned.is_set()
                        advanced.set()
                        review.progress('千牛已登录，正在进入商品发布')
                        logging.getLogger('ecommerce.platforms.qianniu.product_video').warning('模拟视频素材需要人工处理')
                        review.attention('【需要人工处理弹窗】\n当前步骤：等待视频窗口关闭\n完成后输入“继续”。')
                        review.update(outcome='filled')
                        review._wait('已填写，检查后结束', ['finish'], kind='finish')
                        return '仅填写流程完成'

                    with patch('ecommerce.modules.product.web_listing.open_publish', side_effect=fake_open) as publish:
                        page.goto(f'http://127.0.0.1:{server.server_port}/plans/listing')
                        page.locator('#listing-folder').set_input_files(str(folder))
                        expect(page.locator('#listing-import-note')).to_contain_text('自检通过', timeout=10000)
                        expect(page.locator('#listing-submit')).to_be_checked()
                        expect(page.get_by_text('DEMO-1-A', exact=True)).to_be_visible()
                        self.assertEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                        page.locator('#listing-submit').uncheck()
                        page.get_by_role('button', name='仅填写', exact=True).click()
                        dialog = page.get_by_role('dialog', name='等待千牛扫码登录')
                        expect(dialog).to_be_visible(timeout=10000)
                        dialog.get_by_role('button', name='完成', exact=True).click()
                        expect(dialog).to_contain_text('尚未确认登录', timeout=10000)
                        scanned.set()
                        dialog.get_by_role('button', name='关闭人工处理提示').click()
                        self.assertFalse(advanced.wait(.4))
                        expect(page.locator('#listing-phase')).to_have_text('等待扫码登录')
                        page.reload()
                        expect(dialog).to_be_visible(timeout=10000)
                        self.assertFalse(advanced.is_set())
                        dialog.get_by_role('button', name='完成', exact=True).click()
                        video = page.get_by_role('dialog', name='需要人工处理')
                        expect(video).to_contain_text('等待视频窗口关闭', timeout=10000)
                        expect(video).to_contain_text('点击「完成」')
                        expect(page.locator('#listing-log-rows')).to_contain_text('模拟视频素材需要人工处理', timeout=10000)
                        video.get_by_role('button', name='完成', exact=True).click()
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
