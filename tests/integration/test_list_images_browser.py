"""隔离浏览器验证图片真实解码；网络响应全部来自本地样例。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from ecommerce.modules.collection.list_images import collect_list_image
from tests.integration.test_taobao_detail_images_browser import png
from tests.unit.test_list_images import Response


@unittest.skipUnless(os.environ.get('TAOBAO_BROWSER_TESTS') == '1', '显式启用本地浏览器测试')
class ListImagesBrowserTests(unittest.TestCase):
    def test_real_decode_rejects_broken_image_and_preserves_local_copy(self):
        """真实图片解码成功；伪造文件头失败，不覆盖之前已保存的图片。"""
        with tempfile.TemporaryDirectory() as folder, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                # 禁止浏览器访问真实平台；仅在 about:blank 中执行图片解码。
                context.route('**/*', lambda route: route.abort())
                opener = Mock()
                opener.open.side_effect = lambda *args, **kwargs: Response(png(1))
                with patch('ecommerce.modules.collection.list_images.build_opener', return_value=opener):
                    saved = collect_list_image(context, page, 'https://img.alicdn.com/local.png', Path(folder), lambda step: None)
                    self.assertEqual((saved['status'], saved['width'], saved['height']), ('downloaded', 8, 8))
                    original = Path(saved['image_path']).read_bytes()
                    opener.open.side_effect = lambda *args, **kwargs: Response(b'\x89PNG\r\n\x1a\nbroken')
                    failed = collect_list_image(context, page, 'https://img.alicdn.com/broken.png', Path(folder), lambda step: None)
                    self.assertEqual(failed['status'], 'failed')
                    self.assertEqual(Path(saved['image_path']).read_bytes(), original)
                    self.assertEqual(len(list(Path(folder).iterdir())), 1)
            finally:
                browser.close()
