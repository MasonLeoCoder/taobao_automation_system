"""本地页面验证SKU补充信息按名称填写、素材复用、缺失日志及人工弹窗恢复。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from ecommerce.modules.product.assets import SkuImageInputs
from ecommerce.platforms.qianniu import product_sku_media as platform
from ecommerce.platforms.qianniu.popups import PopupGuard
from ecommerce.platforms.qianniu.verification import VerificationGuard
from ecommerce.shared.exceptions import NeedsAttention

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
NAMES = ['规格一【45cm】', '规格二 61cm']
TITLE = '宠物清洁除毛刷沙发地毯通用款'


@unittest.skipUnless(os.environ.get('QIANNIU_BROWSER_TESTS') == '1', '显式启用本地浏览器测试')
class SkuMediaBrowserTests(unittest.TestCase):
    def setUp(self):
        timeout = patch.object(platform, 'WAIT_MS', 3000)
        timeout.start()
        self.addCleanup(timeout.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=os.environ.get('QIANNIU_HEADFUL_TESTS') != '1')
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        picker = (FIXTURES/'sku_media_picker.html').read_text(encoding='utf-8')
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=picker))
        self.page = self.context.new_page()
        self.page.goto('https://qianniu.test/editor')
        self.page.set_content((FIXTURES/'sku_media.html').read_text(encoding='utf-8'))
        self.records = [
            {'name': NAMES[1], 'price': '19.00', 'stock': '20', 'code': '0002'},
            {'name': NAMES[0], 'price': '17.00', 'stock': '10', 'code': '0001'},
        ]
        self.page.evaluate('data=>seedRows(data)', self.records)
        files = {}
        for index, name in enumerate(NAMES):
            file = self.folder/(name+('.PNG' if index == 0 else '.webp'))
            file.write_bytes(b'isolated-image-fixture')
            files[name] = file
        self.images = SkuImageInputs(files, {})

    def guard(self, feedback=None):
        popups = PopupGuard(self.context, feedback, self.folder)
        return VerificationGuard(self.context, feedback, popup_guard=popups)

    def assert_core_unchanged(self):
        records = self.page.evaluate('records')
        for actual, original in zip(records, self.records):
            self.assertEqual({key: actual[key] for key in original}, original)

    def test_all_rows_match_names_reuse_one_upload_for_two_images_and_keep_core_values(self):
        with self.guard():
            result = platform.fill_sku_media(self.page, NAMES, self.images, TITLE)
        self.assertEqual(set(result.filled), set(NAMES))
        self.assertTrue(all(set(fields) == {'SKU搜索主图', '商品规格图片', 'SKU搜索标题'} for fields in result.filled.values()))
        self.assertEqual(result.skipped, {})
        actual = self.page.evaluate('records')
        self.assertEqual(self.page.evaluate('uploadCount'), 2)
        self.assertEqual(self.page.evaluate('saveCount'), 2)
        self.assertEqual(actual[0]['title'], TITLE)
        self.assertEqual(actual[1]['title'], TITLE)
        for row in actual:
            self.assertEqual(row['search'], row['spec'])
        self.assertNotEqual(actual[0]['search'], actual[1]['search'])
        self.assert_core_unchanged()
        # 重复调用保留已有图片，不再次上传；所有跳过信息需要明确记录。
        with self.guard(), self.assertLogs(platform.__name__, level='WARNING'):
            repeated = platform.fill_sku_media(self.page, NAMES, self.images, TITLE)
        self.assertEqual(self.page.evaluate('uploadCount'), 2)
        self.assertTrue(all(fields == ['SKU搜索标题'] for fields in repeated.filled.values()))
        self.assertTrue(all(set(fields) == set(platform.IMAGE_FIELDS) for fields in repeated.skipped.values()))
        self.assert_core_unchanged()

    def test_missing_one_image_still_fills_its_title_and_all_other_sku_fields(self):
        inputs = SkuImageInputs({NAMES[1]: self.images.files[NAMES[1]]}, {NAMES[0]: '未找到同名图片'})
        with self.guard(), self.assertLogs(platform.__name__, level='WARNING') as logs:
            result = platform.fill_sku_media(self.page, NAMES, inputs, TITLE)
        self.assertEqual(result.filled[NAMES[0]], ['SKU搜索标题'])
        self.assertEqual(len(result.filled[NAMES[1]]), 3)
        self.assertEqual(set(result.skipped[NAMES[0]]), set(platform.IMAGE_FIELDS))
        self.assertEqual(self.page.evaluate('uploadCount'), 1)
        self.assertTrue(all(NAMES[0] in line and '未找到同名图片' in line for line in logs.output))
        self.assert_core_unchanged()

    def test_extra_file_requests_after_upload_and_save_stay_intercepted_without_uploading_again(self):
        self.page.evaluate('extraFileRequests=true')
        with self.guard(), self.assertLogs('ecommerce.platforms.qianniu.verification', level='DEBUG') as logs:
            result = platform.fill_sku_media(self.page, NAMES, self.images, TITLE)
        # 没有在测试中另装filechooser监听；必须由生产入口持续接管这六次真实请求。
        intercepted = [line for line in logs.output if '已接管文件选择请求' in line]
        self.assertEqual(len(intercepted), 6)
        self.assertEqual(self.page.evaluate('extraFileRequestsSent'), 4)
        self.assertEqual(self.page.evaluate('uploadCount'), 2)
        self.assertEqual(self.page.evaluate('saveCount'), 2)
        self.assertEqual(sum(map(len, result.filled.values())), 6)
        self.assert_core_unchanged()

    def test_missing_title_still_fills_images(self):
        with self.guard(), self.assertLogs(platform.__name__, level='WARNING') as logs:
            result = platform.fill_sku_media(self.page, NAMES, self.images, None)
        self.assertTrue(all(set(fields) == set(platform.IMAGE_FIELDS) for fields in result.filled.values()))
        self.assertTrue(all(list(fields) == ['SKU搜索标题'] for fields in result.skipped.values()))
        self.assertTrue(all('Excel缺少导购标题' in line for line in logs.output))
        self.assertEqual(self.page.evaluate('uploadCount'), 2)

    def test_missing_everything_and_unmatched_page_row_are_logged_without_opening_drawer(self):
        inputs = SkuImageInputs({}, {name: '图片不存在' for name in NAMES})
        with self.guard(), self.assertLogs(platform.__name__, level='WARNING'):
            result = platform.fill_sku_media(self.page, NAMES+['页面没有的规格'], inputs, None)
        self.assertEqual(result.filled, {})
        self.assertTrue(all(len(fields) == 3 for fields in result.skipped.values()))
        self.assertEqual(self.page.evaluate('openCount'), 0)
        self.assertEqual(self.page.evaluate('uploadCount'), 0)

    def test_wrong_open_drawer_stops_before_writing_another_sku(self):
        self.page.evaluate('openDetail(0)')
        with self.guard(), self.assertRaisesRegex(NeedsAttention, '不是'):
            platform.fill_sku_media(self.page, NAMES, self.images, TITLE)
        self.assertEqual(self.page.evaluate('uploadCount'), 0)
        self.assertEqual(self.page.evaluate('saveCount'), 0)

    def test_unknown_nested_popup_requires_feedback_then_continues_without_reupload(self):
        self.page.evaluate('injectUnknownOnUpload=true')
        messages = []
        def feedback(message):
            messages.append(message)
            self.assertIn('未知公告', message)
            self.assertEqual(self.page.evaluate('uploadCount'), 1)
            self.page.locator('.unknown').evaluate('element=>element.remove()')
        with self.guard(feedback):
            result = platform.fill_sku_media(self.page, NAMES, self.images, TITLE)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.page.evaluate('uploadCount'), 2)
        self.assertEqual(self.page.evaluate('saveCount'), 2)
        self.assertEqual(sum(map(len, result.filled.values())), 6)

    def test_platform_rejecting_title_never_reports_sku_saved(self):
        original = self.page.evaluate('openDetail.toString()')
        self.page.evaluate('source=>{const open=eval("("+source+")");window.openDetail=i=>{open(i);document.querySelector(".sku-component-sku-detail-drawer").insertAdjacentHTML("beforeend",\'<div class="sell-o-message error">标题不符合平台要求</div>\')}}', original)
        with self.guard(), self.assertRaisesRegex(NeedsAttention, '标题不符合平台要求'):
            platform.fill_sku_media(self.page, NAMES, SkuImageInputs({}, {}), TITLE)
        self.assertEqual(self.page.evaluate('saveCount'), 0)

