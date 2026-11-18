"""本地页面验证SKU补充信息按名称填写、素材复用、缺失日志及人工弹窗恢复。"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from ecommerce.modules.product.assets import SkuImageInputs
from ecommerce.platforms.qianniu.product_review import read_listing_fields
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

    def test_layered_split_header_checks_shared_and_distinct_search_images(self):
        self.page.evaluate("""() => {
            const table = document.querySelector('#sell-field-sku table');
            const header = document.createElement('table');
            header.createTHead().innerHTML = '<tr><th>颜色分类</th><th>适用尺码</th><th colspan="8">其他字段</th></tr>';
            table.before(header);
            const original = rowOf;
            rowOf = (record, index, detail) => {
                const row = original(record, index, detail);
                if (!detail) {
                    for (const value of [record.size, record.color]) {
                        const cell = document.createElement('td');
                        cell.textContent = value;
                        row.prepend(cell);
                    }
                } else {
                    row.querySelector('.sku-unique-title-textarea').remove();
                    for (const [label, value] of [['颜色分类', record.color], ['适用尺码', record.size]]) {
                        const cell = row.insertCell();
                        cell.innerHTML = '<div class="sell-component-info-wrapper-wrap">'
                            + '<div class="sell-component-info-wrapper-label-wrap"><span class="sell-component-info-wrapper-label"></span></div>'
                            + '<table><tbody><tr><td class="is-sale-prop"></td></tr></tbody></table></div>';
                        cell.querySelector('.sell-component-info-wrapper-label').textContent = label;
                        cell.querySelector('.is-sale-prop').textContent = value;
                    }
                }
                return row;
            };
            const data = records.map((record, index) => {
                const size = index ? 'M 码' : 'L 码';
                return {...record, size, color:'黑色', name:'黑色 ' + size};
            });
            data.splice(1, 0, {name:'灰色 M 码', size:'M 码', color:'灰色', price:'29', stock:'5', code:'0003'});
            seedRows(data);
        }""")
        names = [*NAMES, '规格三']
        combinations = dict(zip(names, [('黑色', 'M 码'), ('黑色', 'L 码'), ('灰色', 'M 码')]))
        titles = dict(zip(names, ['黑色中号宠物背包', '黑色大号宠物背包', '灰色中号宠物背包']))
        shared = self.images.files[NAMES[0]]
        images = SkuImageInputs({NAMES[0]: shared, NAMES[1]: shared, '规格三': self.images.files[NAMES[1]]}, {})
        # 详情里的颜色与表格不一致时，选图窗口和素材上传都不能开始。
        self.page.evaluate("""() => {
            const original = window.openDetail;
            window.openDetail = index => {
                original(index);
                document.querySelector('.sku-component-sku-detail-drawer td.is-sale-prop').textContent = '错误颜色';
            };
            window.restoreDetail = () => window.openDetail = original;
        }""")
        with self.guard(), self.assertRaisesRegex(NeedsAttention, '详情的颜色和尺码与 Excel 不一致'):
            platform.fill_sku_media(self.page, names, images, TITLE, combinations=combinations)
        self.assertEqual(self.page.evaluate('uploadCount'), 0)
        self.assertEqual(self.page.locator(platform.BATCH).count(), 0)
        self.page.locator(platform.DRAWER).evaluate('drawer => drawer.remove()')
        self.page.evaluate('() => { restoreDetail(); }')
        # 本例验证选图归属，直接给模拟选择器的文件输入供料，避免系统文件窗口的偶发超时。
        chooser = SimpleNamespace(set_files=self.page.frame_locator(platform.PICKER).locator('input[type="file"]').set_input_files)
        with self.guard(), patch.object(platform, 'choose_file_dialog', return_value=chooser):
            result = platform.fill_sku_media(self.page, names, images, TITLE,
                                             sku_titles=titles, combinations=combinations)
        self.assertEqual(result.skipped, {})
        self.assertEqual(self.page.evaluate('uploadCount'), 2)
        rows = self.page.evaluate('records')
        self.assertEqual([(row['code'], row['title']) for row in rows],
                         [('0002', titles[NAMES[1]]), ('0003', titles['规格三']), ('0001', titles[NAMES[0]])])
        self.assertEqual(rows[0]['search'], rows[2]['search'])
        self.assertNotEqual(rows[0]['search'], rows[1]['search'])
        self.assertTrue(all(row.get('search') and not row.get('spec') for row in rows))
        self.assertEqual([(row['price'], row['stock']) for row in rows],
                         [('19.00', '20'), ('29', '5'), ('17.00', '10')])

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

    def test_cj010_crop_matches_uploaded_material_in_both_image_slots(self):
        self.page.evaluate("""() => {
            const base = 'https://img.alicdn.com/imgextra/i1/2222092663651/O1CN01gK7KOFS2yvD0jwnE';
            const original = base + '_!!2222092663651.jpg';
            const cropped = base + '~crop,0,17,365,365~_!!2222092663651.jpg';
            assets.push = asset => Array.prototype.push.call(assets, {...asset, source:original});
            for (const key of ['chooseSearch', 'chooseSpec']) {
                const choose = window[key];
                window[key] = () => choose(cropped);
            }
        }""")
        inputs = SkuImageInputs({NAMES[0]: self.images.files[NAMES[0]]}, {})
        with self.guard():
            result = platform.fill_sku_media(self.page, [NAMES[0]], inputs, TITLE)
        self.assertEqual(result.filled[NAMES[0]], [*platform.IMAGE_FIELDS, 'SKU搜索标题'])
        self.assertEqual(result.skipped, {})
        self.assertIn('~crop,0,17,365,365~', self.page.evaluate('records[1].search'))
        self.assertEqual(self.page.evaluate('uploadCount'), 1)

    def test_wrong_search_image_can_be_replaced_manually_or_skipped(self):
        for action in ('continue', 'skip_image', 'timeout'):
            with self.subTest(action=action):
                self.page.evaluate("""data => {
                    if (!window.realChooseSearch) window.realChooseSearch = chooseSearch;
                    assets.length = 0;
                    seedRows(data);
                    chooseSearch = source => realChooseSearch(currentIndex === 1 ? source.replace('/assets/', '/wrong/') : source);
                }""", self.records)
                messages = []
                def feedback(page, message, deadline):
                    messages.append((message, deadline))
                    self.assertIn(NAMES[0], message)
                    self.assertIn('SKU搜索主图', message)
                    self.assertIn('素材不一致', message)
                    if action == 'continue' and len(messages) == 2:
                        page.evaluate("realChooseSearch('https://qianniu.test/assets/人工.jpg')")
                        page.locator(platform.BATCH).get_by_role('button', name='确定').click()
                    return action

                with self.guard():
                    result = platform.fill_sku_media(self.page, NAMES, self.images, TITLE, on_image_review=feedback)
                rows = self.page.evaluate('records')
                self.assertEqual([row['title'] for row in rows], [TITLE, TITLE])
                self.assertTrue(all(row.get('spec') for row in rows))
                self.assertIsNotNone(rows[0].get('search'))
                self.assertEqual(result.differences, [])
                if action == 'continue':
                    self.assertEqual(len(messages), 2)
                    self.assertEqual(messages[0][1], messages[1][1])
                    self.assertIn('还未读到', messages[1][0])
                    self.assertEqual(rows[1]['search'], 'https://qianniu.test/assets/人工.jpg')
                    self.assertEqual(result.skipped, {})
                else:
                    self.assertEqual(result.optional_skips, {NAMES[0]: {'SKU搜索主图'}})
                    self.assertIn('自动跳过' if action == 'timeout' else '用户选择', result.skipped[NAMES[0]]['SKU搜索主图'])
                    self.assertIsNone(rows[1].get('search'))
                self.assert_core_unchanged()

    def test_spec_picker_closes_on_timeout_and_next_sku_continues(self):
        fill = platform._fill_spec_image
        def fail_first(page, drawer, file, uploaded_assets):
            if file == self.images.files[NAMES[0]]:
                drawer.locator(platform.SPEC_IMAGE).locator('.empty-container').click()
                raise platform._ImageError('素材上传等待超时')
            return fill(page, drawer, file, uploaded_assets)

        with self.guard(), patch.object(platform, '_fill_spec_image', side_effect=fail_first):
            result = platform.fill_sku_media(self.page, NAMES, self.images, TITLE,
                                             on_image_review=lambda *args: 'timeout')
        rows = self.page.evaluate('records')
        self.assertEqual(result.optional_skips, {NAMES[0]: {'商品规格图片'}})
        self.assertEqual(result.differences, [])
        self.assertTrue(rows[1]['search'])
        self.assertFalse(rows[1].get('spec'))
        self.assertTrue(rows[0]['spec'])
        self.assertEqual([row['title'] for row in rows], [TITLE, TITLE])

    def test_two_skus_can_use_one_uploaded_image_with_separate_titles(self):
        images = SkuImageInputs(dict.fromkeys(NAMES, self.images.files[NAMES[0]]), {})
        titles = dict(zip(NAMES, ('中号宠物背包', '大号宠物背包')))
        with self.guard():
            result = platform.fill_sku_media(self.page, NAMES, images, TITLE, sku_titles=titles)
        self.assertEqual(result.skipped, {})
        self.assertEqual(self.page.evaluate('uploadCount'), 1)
        rows = self.page.evaluate('records')
        self.assertEqual([row['title'] for row in rows], [titles[name] for name in reversed(NAMES)])
        self.assertEqual(rows[0]['search'], rows[1]['search'])
        self.assertTrue(rows[0]['search'])
        self.assert_core_unchanged()

    def test_one_spec_uses_its_title_and_blank_spec_uses_final_guide(self):
        title = '规格一宠物除毛刷'
        inputs = SkuImageInputs({}, {})
        with self.guard():
            result = platform.fill_sku_media(self.page, NAMES, inputs, TITLE,
                                             sku_titles={NAMES[0]:title, NAMES[1]:''})
            self.page.evaluate('''() => {
                for (const [id, value] of [['title','测试标题'], ['shopping_title','导购标题'],
                                           ['price','17'], ['outerId','DEMO']]) {
                    const group = document.createElement('div');
                    group.id = 'sell-field-' + id;
                    const field = document.createElement('input');
                    field.value = value;
                    group.append(field);
                    document.body.append(group);
                }
            }''')
            observed = read_listing_fields(self.page)
        self.assertEqual({row['name']: row['title'] for row in self.page.evaluate('records')},
                         {NAMES[0]:title, NAMES[1]:TITLE})
        self.assertEqual({name: values for name, values, _ in observed['sku_media']},
                         {NAMES[0]:[title], NAMES[1]:[TITLE]})
        self.assertTrue(all(fields == ['SKU搜索标题'] for fields in result.filled.values()))

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

