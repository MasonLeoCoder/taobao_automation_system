"""商品工作台隔离验证：上传、只填、入仓意图和人工恢复。"""
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook
from PIL import Image
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.modules.product.listing_profiles import BACKPACK, BACKPACK_CATEGORY
from ecommerce.modules.product.listing_logs import log_page, local_log
from ecommerce.modules.product.listing_templates import template_for
from ecommerce.modules.product.web_listing import _ListingRun
from ecommerce.modules.product.web_materials import CATEGORY, import_materials
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.web.app import create_app
from tests.qianniu_fixtures import ACCOUNT, IDENTITY, TARGET, TARGET_FORM, fixture_session_file


EDITOR_URL = 'https://item.upload.taobao.com/sell/v2/publish.htm?fixture=1'


def fixture_settings(root):
    """构造非验证库专用标识的隔离文件后端；不连接MySQL。"""
    settings = load_settings(root=root)
    return replace(settings, environment='', mysql=replace(settings.mysql, database='listing_isolated'))


@contextmanager
def bind_listing_fixture(app):
    """保留真实的启动校验，使用测试账号资料及本地任务存储。"""
    workbench = app.extensions['product_listing']
    settings = replace(app.config['SETTINGS'], task_backend='mysql')
    fixture_session_file(settings)
    account = dict(ACCOUNT)

    def run_locally(config, name, action, *, qianniu_scope):
        assert qianniu_scope == TARGET
        return run_task(replace(config, task_backend='sqlite'), name, action)

    with (patch.dict(app.config, {'SETTINGS': settings}),
          patch.object(workbench, 'settings', replace(workbench.settings, task_backend='mysql')),
          patch('ecommerce.modules.account.managed.AccountStore') as store,
          patch('ecommerce.modules.product.web_listing.run_task', side_effect=run_locally),
          patch('ecommerce.modules.product.listing_history.cli_listing_events', return_value=[]),
          patch('ecommerce.modules.product.web_listing.local_log',
                side_effect=lambda config, path: local_log(replace(config, task_backend='sqlite'), path)),
          patch('ecommerce.modules.product.web_listing.log_page',
                side_effect=lambda config, *args, **kwargs: log_page(replace(config, task_backend='sqlite'), *args, **kwargs))):
        store.return_value.list_accounts.return_value = [account]
        yield account


def fixture_materials(root):
    """生成脱敏可导入目录；参数：临时根目录；返回：样例商品文件夹。"""
    folder = root / '任意商品名'
    images = folder / '1-1主图'
    images.mkdir(parents=True)
    book = Workbook()
    book.active.title = 'Sheet1'
    for row in (('宝贝标题', '导购标题', '商家编码', '分类', '一口价'),
                ('脱敏测试粘毛器', '十个中文字导购标题测试', 'DEMO-1', '猫用品', 17),
                ('销售规格', '价格', '数量', '商家编码'), ('白色', 17.5, 3, 'DEMO-1-A')):
        book.active.append(row)
    book.save(folder / '任意表名.xlsx')
    book.close()
    for i in range(1, 6):
        Image.new('RGB', (24, 24), 'white').save(images / f'{i}.png')
    return folder


class WebListingTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = fixture_materials(self.root)
        self.app = create_app(fixture_settings(self.root))
        self.client = self.app.test_client()
        self.headers = {'X-Collection-Token': self.app.config['COLLECTION_TOKEN']}
        self.account = self.enterContext(bind_listing_fixture(self.app))
        self.open_publish = self.enterContext(patch('ecommerce.modules.product.web_listing.open_publish'))

    def preflight(self, *, bad_image=False, excel=None, video=None, offline=False, **target):
        """走真实导入接口；参数：是否破坏图片内容；返回：HTTP响应。"""
        images = [(BytesIO(b'fake' if bad_image and p.stem == '2' else p.read_bytes()), p.name)
                  for p in sorted((self.folder / '1-1主图').iterdir())]
        if excel is None:
            excel = BytesIO((self.folder / '任意表名.xlsx').read_bytes())
        return self.client.post('/api/listing/preflight', headers=self.headers, data={
            'keyword': '粘毛器', 'brand': '无品牌/无注册商标', 'category': CATEGORY,
            'excel': (excel, '任意表名.xlsx'), 'main': images, **({} if offline else {**TARGET_FORM, **target}),
            **({'video': (BytesIO(video), '商品视频.mp4')} if video is not None else {})})

    def test_managed_preflight_seals_account_and_shop_and_rejects_changed_target(self):
        workbench = self.app.extensions['product_listing']
        other_shop = 'd' * 32
        self.assertEqual(self.preflight(shop_id=other_shop).status_code, 409)
        result = self.preflight()
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json['listing_target'], TARGET)
        identifier = result.json['id']
        url = f'/api/listing/{identifier}/start'
        self.assertEqual(self.client.post(url, headers=self.headers, json={'submit': False}).status_code, 409)
        self.assertEqual(self.start_request(url, json=dict(shop_id=other_shop, submit=False)).status_code, 409)
        self.assertFalse((workbench.root / identifier / 'state.json').exists())
        self.account['login_required'] = True
        self.assertEqual(self.start_request(url, json={'submit': False}).status_code, 409)
        self.open_publish.assert_not_called()

    def start_request(self, url, *, json):
        return self.client.post(url, headers=self.headers, json={**TARGET_FORM, **json})

    def test_file_environment_allows_offline_preview_and_existing_history_only(self):
        workbench = self.app.extensions['product_listing']
        offline = replace(workbench.settings, task_backend='sqlite')
        with (patch.dict(self.app.config, {'SETTINGS': offline}), patch.object(workbench, 'settings', offline)):
            result = self.preflight(offline=True)
            self.assertEqual(result.status_code, 201)
            identifier = result.json['id']
            url = f'/api/listing/{identifier}'
            reply = self.client.post(url + '/start', headers=self.headers, json={'submit': False})
            self.assertEqual(reply.status_code, 409)
            self.assertIn('启用 MySQL', reply.json['error'])
            self.assertEqual(self.start_request(url + '/start', json={'submit': True}).status_code, 409)
            history = workbench.root / identifier / 'state.json'
            self.assertFalse(history.exists())
            self.open_publish.assert_not_called()
            task_id = 'f' * 32
            history.write_text(json.dumps({'phase':'succeeded', 'outcome':'filled', 'task_id':task_id}), encoding='utf-8')
            self.assertEqual(self.client.get(url).json['task']['outcome'], 'filled')
            listing_history = self.client.get('/tasks?view=listing')
            self.assertIn(f'/tasks/{task_id}', listing_history.get_data(as_text=True))
            task_page = self.client.get(f'/tasks/{task_id}', follow_redirects=True)
            self.assertEqual(task_page.status_code, 200)
            self.assertEqual(task_page.request.path, '/plans/listing')
            self.assertEqual(task_page.request.args['job'], identifier)

    def test_listing_history_filters_and_preserves_attribution(self):
        workbench = self.app.extensions['product_listing']
        for digit, title, target in (('1', '历史商品', None), ('2', '当前商品', {
                'account_id': 'a' * 32, 'shop_id': 'b' * 32,
                'login_name': 'tb123:测试', 'shop_name': '测试店铺', 'platform_shop_id': '123'})):
            folder = workbench.root / (digit * 32)
            folder.mkdir(parents=True)
            (folder / 'manifest.json').write_text(json.dumps(dict(
                preview={'title': title, 'code': f'CODE-{digit}'}, listing_target=target)), encoding='utf-8')
            (folder / 'state.json').write_text(json.dumps(dict(
                task_id=digit * 32, phase='succeeded', outcome='warehouse')), encoding='utf-8')
        old = self.client.get('/tasks?view=listing&code=code-1')
        self.assertEqual(old.status_code, 200)
        self.assertIn('未知', old.get_data(as_text=True))
        current = self.client.get('/tasks?view=listing&account=TB123&shop=123&outcome=warehouse')
        self.assertEqual(current.status_code, 200)
        self.assertIn('当前商品', current.get_data(as_text=True))
        self.assertNotIn('历史商品', current.get_data(as_text=True))

    def test_unavailable_editor_is_reported_before_warehouse_click(self):
        login_frame = MagicMock(url='https://loginmyseller.taobao.com/')
        login_frame.locator.return_value.all.return_value = [MagicMock(is_visible=lambda: True)]
        cases = (
            (EDITOR_URL, True, [], '编辑页已关闭'),
            (login_frame.url, False, [login_frame], '会话已失效'),
            ('https://myseller.taobao.com/home.htm/', False, [], '已离开商品编辑页'),
        )
        for url, closed, frames, message in cases:
            with self.subTest(message=message):
                identifier = self.preflight().json['id']
                page = MagicMock(url=url, is_closed=lambda: closed, frames=frames)
                click = MagicMock()

                def fake_open(*args, web_review, **kwargs):
                    web_review.before_click(page)
                    web_review.click_intent()
                    click()

                self.open_publish.side_effect = fake_open
                with patch('ecommerce.modules.product.web_listing.read_listing_fields') as read:
                    response = self.start_request(f'/api/listing/{identifier}/start', json={'submit': True})
                    self.assertEqual(response.status_code, 202)
                    final = self.state(identifier, lambda state: state['phase'] == 'needs_attention')
                    read.assert_not_called()
                self.assertIn(message, final['message'])
                self.assertFalse(final['submit_attempted'])
                page.context.new_page.assert_not_called()
                click.assert_not_called()

    def test_uncertain_submission_warning_uses_actual_code_and_the_same_shop(self):
        worker = MagicMock(spec=_ListingRun)
        worker.target = {'shop_id': 's'}
        worker.path = self.root / ('a' * 32)
        worker.settings = self.app.extensions['product_listing'].settings
        worker.state = {'task_id': 'a' * 32}
        worker._wait.return_value = 'approve'
        rows = [{'id': digit * 32, 'task_id': digit * 32, 'target': {'shop_id': shop},
                 'outcome': outcome, 'code': code, 'created_at': ''} for digit, shop, outcome, code in (
                     ('a', 's', 'uncertain', 'actual'), ('b', 's', 'uncertain', 'actual'),
                     ('c', 't', 'uncertain', 'actual'), ('d', 's', 'warehouse', 'actual'),
                     ('e', 's', 'uncertain', 'planned'))]
        with patch('ecommerce.modules.product.listing_history.listing_records', return_value=iter(rows)):
            _ListingRun.warn_pending_submission(worker, MagicMock(), 'actual')
        self.assertEqual(worker._wait.call_args.kwargs['related_tasks'], ['b' * 32])
        worker.update.assert_called_once_with(submitted_code='actual')

    def test_adapted_video_preview_and_worker_use_the_same_sealed_copy(self):
        def prepare(source, adapted):
            adapted.parent.mkdir()
            adapted.write_bytes(b'confirmed adapted video')
            return {'source_ratio': '16:9', 'upload_ratio': '1:1', 'duration': 6, 'adapted': True}

        with patch('ecommerce.modules.product.web_materials.prepare_listing_video', side_effect=prepare):
            result = self.preflight(video=b'original video')
        self.assertEqual(result.status_code, 201)
        identifier = result.json['id']
        self.assertEqual(result.json['preview']['video']['upload_ratio'], '1:1')
        with self.client.get(f'/api/listing/{identifier}/video') as response:
            self.assertEqual(response.data, b'confirmed adapted video')
        self.open_publish.return_value = '已填写'
        self.start_request(f'/api/listing/{identifier}/start', json={'submit': False})
        self.state(identifier, lambda state: state['phase'] == 'succeeded')
        video_path = Path(self.open_publish.call_args.kwargs['video_file'])
        self.assertEqual(video_path.read_bytes(), b'confirmed adapted video')
        self.assertEqual(video_path.parent.parent.joinpath('video', '商品视频.mp4').read_bytes(), b'original video')

    def test_download_bundle_and_validate_filled_template(self):
        response = self.client.get('/api/listing/template', query_string={'category': CATEGORY})
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertEqual(response.mimetype, 'application/zip')
        with ZipFile(BytesIO(response.data)) as archive:
            prefix = '粘毛器-商品资料包/'
            self.assertEqual(set(archive.namelist()), {prefix, *(prefix + path for path in
                ('1-1主图/', '3-4主图/', 'sku图/', '详情图/', '产品信息.xlsx', '填写说明.txt'))})
            self.assertIn('商品视频.mp4', archive.read(prefix + '填写说明.txt').decode())
            data = archive.read(prefix + '产品信息.xlsx')
        response.close()
        workbook = load_workbook(BytesIO(data))
        self.addCleanup(workbook.close)
        self.assertEqual(workbook.sheetnames, ['Sheet1', '填写示例', '属性选项', '填写说明'])
        entry = workbook['Sheet1']
        self.assertTrue(all(cell.value is None for cell in entry[2]))
        self.assertEqual(len(entry.data_validations.dataValidation), 5)
        empty = self.preflight(excel=BytesIO(data))
        self.assertEqual(empty.status_code, 422)
        example = workbook['填写示例']
        for number in (2, 5, 6):
            for cell in example[number]:
                entry[cell.coordinate] = cell.value

        def submit_workbook():
            stream = BytesIO()
            workbook.save(stream)
            stream.seek(0)
            return self.preflight(excel=stream)

        result = submit_workbook()
        self.assertEqual(result.status_code, 201, result.json)
        self.assertEqual(result.json['preview']['shop_categories'], ['猫毛清洁', '家居清洁'])
        self.assertEqual(len(result.json['preview']['skus']), 2)
        entry['F2'] = '未知场景'
        rejected = submit_workbook()
        self.assertEqual(rejected.status_code, 422)
        self.assertIn('「适用场景」的值「未知场景」', rejected.json['errors'][0])
        entry['F2'] = '地毯'
        workbook['填写说明']['D1'] = 99
        self.assertIn('模板与当前类目或版本不一致', submit_workbook().json['errors'][0])
        self.assertEqual(self.client.get('/api/listing/template', query_string={'category': '其他类目'}).status_code, 404)
        self.open_publish.assert_not_called()

    def test_preflight_reports_missing_main_image_number(self):
        (self.folder / '1-1主图' / '5.png').unlink()
        result = self.preflight()
        self.assertEqual(result.status_code, 422)
        self.assertIn('1-1主图：需要 5 张，已找到 4 张', result.json['errors'][0])
        self.assertIn('缺少编号 5', result.json['errors'][0])
        self.assertTrue(any('视频：未提供，将跳过' in warning for warning in result.json['warnings']))

    def test_backpack_combined_skus_allow_warehouse_after_profile_readback(self):
        workbench = self.app.extensions['product_listing']
        binding = {'id': 12, 'platform_id': None, 'profile': BACKPACK.key}
        book = load_workbook(template_for(BACKPACK.key, BACKPACK_CATEGORY))
        self.addCleanup(book.close)
        entry = book['Sheet1']
        self.assertEqual(len(entry.data_validations.dataValidation), 5)
        for row in (2, 5, 6):
            for cell in book['填写示例'][row]:
                entry[cell.coordinate] = cell.value

        def check(sku_images=()):
            stream = BytesIO()
            book.save(stream)
            stream.seek(0)
            images = [(path.name, BytesIO(path.read_bytes())) for path in (self.folder / '1-1主图').iterdir()]
            return import_materials(workbench.root, {'excel': [('产品信息.xlsx', stream)], 'main': images,
                                                     'sku': list(sku_images)},
                                    keyword='宠物背包', category=BACKPACK_CATEGORY, brand='无品牌/无注册商标',
                                    profile=BACKPACK, binding=binding, target=TARGET)

        entry['I2'] = None
        self.assertIn('「适用品种」为必填', check()['errors'][0])
        entry['I2'] = '猫' * 101
        self.assertIn('最多 100 个字符', check()['errors'][0])
        entry['I2'] = '英国短毛猫'
        entry['J2'] = None
        entry['K2'] = None
        entry['A6'] = '灰色 L 码'
        result = check()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['preview']['sku_mode'], '单层展示')
        self.assertEqual([row['销售规格'] for row in result['preview']['skus']], ['黑色 M 码', '灰色 L 码'])
        shared = '双肩背包【配色】'
        entry['G5'] = entry['G6'] = shared
        no_image = check()
        self.assertEqual(no_image['errors'], [])
        self.assertIn(shared, no_image['preview']['sku_image_issues']['黑色 M 码'])
        sample = next((self.folder / '1-1主图').iterdir()).read_bytes()
        with_image = check([(shared + '.png', BytesIO(sample))])
        self.assertEqual(with_image['errors'], [])
        self.assertEqual(with_image['preview']['sku_images'], {
            '黑色 M 码': shared + '.png', '灰色 L 码': shared + '.png',
        })
        self.assertNotIn('allow_submit', result['preview'])
        identifier = result['id']
        manifest = workbench.root / identifier / 'manifest.json'
        saved = json.loads(manifest.read_text(encoding='utf-8'))
        saved['preview']['allow_submit'] = False
        manifest.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        self.assertNotIn('allow_submit', self.client.get(f'/api/listing/{identifier}').json['preview'])

        preview = result['preview']
        actual = {key: preview[key] for key in ('title', 'guide', 'price', 'code', 'skus')}
        actual['attributes'] = {key: value or None for key, value in preview['attributes'].items()}
        actual['attributes']['品牌'] = preview['brand']
        actual['sku_media'] = [[row['销售规格'], [preview['guide']] if preview['guide'] else [], []]
                               for row in preview['skus']]
        attributes = {key: SimpleNamespace(selected=value, fallback_reason=None)
                      for key, value in preview['attributes'].items() if value}

        def read(page, *, attribute_labels):
            self.assertEqual(attribute_labels, (*BACKPACK.attributes, '品牌'))
            return {**actual, 'attributes':dict(actual['attributes'])}

        def open_and_review(settings, *, web_review, submit, **kwargs):
            self.assertTrue(submit)
            page = MagicMock(url=EDITOR_URL, is_closed=lambda: False, frames=[])
            web_review.authenticate_managed(page)
            web_review.filled(page, None, None, attributes)
            actual['attributes']['品牌'] = None
            with self.assertRaisesRegex(NeedsAttention, '品牌、必填属性或 SKU 结构'):
                web_review.before_click(page)
            actual['attributes']['品牌'] = preview['brand']
            web_review.before_click(page)
            web_review.click_intent()
            web_review.click_finished()
            web_review.result(SimpleNamespace(status='succeeded', message='模拟已入仓', item_id='123456', page=page))
            page.context.new_page.assert_not_called()
            return '模拟已入仓'

        self.open_publish.side_effect = open_and_review
        with (patch('ecommerce.modules.product.web_listing.read_listing_fields', side_effect=read),
              patch('ecommerce.modules.product.web_listing.verify_session', return_value=IDENTITY) as authenticate):
            response = self.start_request(f'/api/listing/{identifier}/start', json={'submit': True})
            self.assertEqual(response.status_code, 202)
            final = self.state(identifier, lambda state: state['phase'] == 'succeeded')
        authenticate.assert_called_once()
        self.assertEqual(final['verified_identity']['login_name'], IDENTITY.login_name)
        self.assertTrue(final['submit_attempted'])
        self.assertEqual((final['outcome'], final['item_id']), ('warehouse', '123456'))
        self.open_publish.assert_called_once()

    def state(self, identifier, condition):
        """等待本机后台状态；参数：导入编号、判定；返回：对应任务快照。"""
        end = time.monotonic() + 6
        while time.monotonic() < end:
            state = self.client.get(f'/api/listing/{identifier}').json['task']
            if state and condition(state):
                return state
            time.sleep(.025)
        self.fail(f'未等到状态：{state}')

    def send(self, identifier, state, action):
        """反馈当前提示；参数：导入编号、快照和决定；返回：HTTP响应。"""
        return self.client.post(f'/api/listing/{identifier}/feedback', headers=self.headers,
                                json={'prompt_id': state['prompt']['id'], 'action': action})

    def test_invalid_file_never_creates_runnable_draft(self):
        response = self.preflight(bad_image=True)
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(response.json['id'])
        self.assertIn('2.png', response.json['errors'][0])
        self.open_publish.assert_not_called()

    def test_preflight_retries_windows_directory_lock_and_reports_persistent_failure(self):
        rename = Path.rename
        error = PermissionError('目录暂时占用')
        error.winerror = 5
        blocked = 1

        def rename_bundle(source, destination):
            nonlocal blocked
            if blocked:
                blocked -= 1
                raise error
            return rename(source, destination)

        with patch.object(Path, 'rename', rename_bundle), patch('ecommerce.modules.product.web_materials.time.sleep'):
            self.assertEqual(self.preflight().status_code, 201)
            blocked = 3
            response = self.preflight()
        self.assertEqual(response.status_code, 503)
        self.assertIn('暂时无法保存导入资料', response.json['error'])
        self.assertEqual(len(list(self.app.extensions['product_listing'].root.iterdir())), 1)
        self.open_publish.assert_not_called()

    def test_edited_titles_are_checked_and_frozen_for_the_worker(self):
        identifier = self.preflight().json['id']
        url = f'/api/listing/{identifier}/start'
        titles = {'title': '猫' * 30, 'guide': '毛' * 15}
        for invalid in ({**titles, 'title': '猫' * 31}, {**titles, 'guide': '毛' * 16},
                        {**titles, 'title': '猫\n毛'}, {**titles, 'guide': '太短'}):
            response = self.start_request(url, json={'submit': False, 'titles': invalid})
            self.assertEqual(response.status_code, 422)
        self.open_publish.assert_not_called()
        self.open_publish.return_value = '填写完成'
        response = self.start_request(url, json={'submit': False, 'titles': titles})
        self.assertEqual(response.status_code, 202)
        self.assertEqual({key: response.json['preview'][key] for key in titles}, titles)
        self.state(identifier, lambda state: state['phase'] == 'succeeded')
        self.assertEqual(self.open_publish.call_args.kwargs['title_overrides'], titles)

    def test_edited_sku_title_is_validated_and_frozen_under_spec_name(self):
        identifier = self.preflight().json['id']
        url = f'/api/listing/{identifier}/start'
        for titles in ({'sku-2':'未知规格'}, {'sku-1':'猫' * 15 + 'A'}):
            response = self.start_request(url, json={'submit':False, 'sku_titles':titles})
            self.assertEqual(response.status_code, 422)
        self.open_publish.assert_not_called()
        self.open_publish.return_value = '填写完成'
        title = '猫' * 15
        data = {'submit':False, 'sku_titles':{'sku-1':title[:5] + '　' + title[5:]}}
        response = self.start_request(url, json=data)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json['sku_titles'], {'sku-1': title})
        self.state(identifier, lambda state: state['phase'] == 'succeeded')
        self.assertEqual(self.open_publish.call_args.kwargs['sku_titles'], {'白色':title})

    def test_read_only_preflight_and_submit_off_preserve_single_task(self):
        data = self.preflight().json
        self.assertEqual(data['preview']['skus'][0]['价格'], '17.5')
        self.assertIn('video', data['skipped'])
        self.open_publish.assert_not_called()

        def fake_open(*args, **kwargs):
            self.assertFalse(kwargs['submit'])
            review = kwargs['web_review']
            review.update(outcome='filled')
            review._wait('结束检查', ['finish'])
            return '已填写，未提交'

        self.open_publish.side_effect = fake_open
        identifier = data['id']
        endpoint = f'/api/listing/{identifier}/start'
        self.assertEqual(self.start_request(endpoint, json={'submit': False}).status_code, 202)
        paused = self.state(identifier, lambda state: state['phase'] == 'waiting')
        repeated = self.start_request(endpoint, json={'submit': True}).json
        self.assertFalse(repeated['task']['submit'])
        self.assertEqual(paused['task_id'], repeated['task']['task_id'])
        self.assertEqual(self.send(identifier, paused, 'approve').status_code, 409)
        self.assertEqual(self.send(identifier, paused, 'finish').status_code, 200)
        finished = self.state(identifier, lambda state: state['phase'] == 'succeeded')
        self.assertEqual((finished['outcome'], finished['submit_attempted']), ('filled', False))
        logs = self.client.get(f'/api/listing/{identifier}/logs').json
        self.assertTrue(any('结束检查' in row['message'] for row in logs['events']))
        self.assertEqual(self.client.get(f'/api/listing/{identifier}/logs?after={logs["next_cursor"]}').json['events'], [])
        self.assertEqual(self.client.get(f'/api/listing/{identifier}/logs?before=0').status_code, 400)
        other = self.preflight().json['id']
        self.assertEqual(self.client.get(f'/api/listing/{other}/logs').json['events'], [])
        self.open_publish.assert_called_once()

    def test_native_prompt_during_verification_restores_pending_step(self):
        identifier = self.preflight().json['id']
        native_requested = Event()
        native_answer = []

        def fake_open(*args, **kwargs):
            review = kwargs['web_review']

            def pump(ms):
                if native_requested.wait(ms / 1000) and not native_answer:
                    native_answer.append(review.attention('【需要人工决定原生弹窗】'))

            page = SimpleNamespace(is_closed=lambda: False, wait_for_timeout=pump, frames=[], screenshot=lambda **_: None)
            review.verification(SimpleNamespace(page=page, step='验证商品编辑页'))
            return '登录完成'

        self.open_publish.side_effect = fake_open
        self.start_request(f'/api/listing/{identifier}/start', json={'submit': False})
        login = self.state(identifier, lambda state: state['prompt'] and state['prompt']['kind'] == 'manual')
        native_requested.set()
        native = self.state(identifier, lambda state: state['prompt'] and state['prompt']['kind'] == 'native')
        self.assertEqual(self.send(identifier, login, 'continue').status_code, 409)
        self.assertEqual(self.send(identifier, native, 'cancel').status_code, 200)
        restored = self.state(identifier, lambda state: state['prompt'] == login['prompt'])
        self.assertEqual(self.send(identifier, restored, 'continue').status_code, 200)
        self.state(identifier, lambda state: state['phase'] == 'succeeded')
        self.assertEqual(native_answer, ['取消'])

    def test_mismatch_requires_approval_and_unknown_can_finish_without_replay(self):
        data = self.preflight().json
        identifier = data['id']
        preview = data['preview']
        actual = {k: preview[k] for k in ('title', 'guide', 'code', 'skus', 'attributes')}
        actual.update(price='18', sku_media=[['白色', [preview['guide']], []]])

        def fake_open(*args, **kwargs):
            review = kwargs['web_review']
            review.sku_media = list(actual['sku_media'])
            page = SimpleNamespace(url=EDITOR_URL, is_closed=lambda: False, wait_for_timeout=lambda ms: time.sleep(ms / 1000), frames=[],
                                   screenshot=lambda **opts: Path(opts['path']).write_bytes(b'fixture'))
            review.before_click(page)
            review.click_intent()
            review.submission_attention('千牛结果未确认')
            return '不可到达'

        self.open_publish.side_effect = fake_open
        with patch('ecommerce.modules.product.web_listing.read_listing_fields', side_effect=lambda page: dict(actual)):
            response = self.start_request(f'/api/listing/{identifier}/start', json={'submit': True})
            self.assertEqual(response.status_code, 202)
            paused = self.state(identifier, lambda state: state['prompt'] and 'approve' in state['prompt']['actions'])
            self.assertFalse(paused['submit_attempted'])
            self.assertIn('页面值「18」', paused['prompt']['differences'][0])
            actual['price'] = '19'
            self.send(identifier, paused, 'approve')
            again = self.state(identifier, lambda state: state['prompt'] and state['prompt']['id'] != paused['prompt']['id'])
            self.assertFalse(again['submit_attempted'])
            self.assertIn('页面值「19」', again['prompt']['differences'][0])
            self.send(identifier, again, 'approve')
            unknown = self.state(identifier, lambda state: state['outcome'] == 'uncertain' and state['prompt'])
            self.send(identifier, unknown, 'finish')
            final = self.state(identifier, lambda state: state['phase'] == 'needs_attention')
        self.assertTrue(final['submit_attempted'])
        self.assertEqual(final['outcome'], 'uncertain')
        self.open_publish.assert_called_once()

    def test_title_differences_require_correction_before_warehouse_click(self):
        data = self.preflight().json
        identifier, preview = data['id'], data['preview']
        actual = {k: preview[k] for k in ('title', 'guide', 'code', 'skus', 'attributes', 'price')}
        actual.update(title='', guide='变更后的导购标题', sku_media=[['白色', ['去填写'], []]])

        def fake_open(*args, **kwargs):
            review = kwargs['web_review']
            review.sku_media = list(actual['sku_media'])
            page = SimpleNamespace(url=EDITOR_URL, is_closed=lambda: False, wait_for_timeout=lambda ms: time.sleep(ms / 1000), frames=[],
                                   screenshot=lambda **opts: Path(opts['path']).write_bytes(b'fixture'))
            review.before_click(page)
            review.click_intent()
            return '模拟入仓'

        self.open_publish.side_effect = fake_open
        with patch('ecommerce.modules.product.web_listing.read_listing_fields', side_effect=lambda page: dict(actual)):
            response = self.start_request(f'/api/listing/{identifier}/start', json={'submit': True})
            self.assertEqual(response.status_code, 202)
            paused = self.state(identifier, lambda state: state['prompt'] and state['prompt']['kind'] == 'titles')
            self.assertFalse(paused['submit_attempted'])
            self.assertEqual(len(paused['prompt']['differences']), 3)
            self.assertIn('SKU「白色」搜索标题', paused['prompt']['differences'][2])
            self.assertEqual(self.send(identifier, paused, 'approve').status_code, 409)
            self.send(identifier, paused, 'recheck_titles')
            again = self.state(identifier, lambda state: state['prompt'] and state['prompt']['id'] != paused['prompt']['id'])
            self.assertFalse(again['submit_attempted'])
            actual.update(title=preview['title'], guide=preview['guide'], sku_media=[['白色', [preview['guide']], []]])
            self.send(identifier, again, 'recheck_titles')
            final = self.state(identifier, lambda state: state['phase'] == 'succeeded')
        self.assertTrue(final['submit_attempted'])

    def test_specification_and_main_image_review_can_end_the_task(self):
        cases = [
            ('specifications', 'specifications_wait', '适用尺码多出「大号」'),
            ('main_image', 'main_image_wait', '3:4 主图已核对 2/5，正在填写第 3 格'),
        ]
        for kind, method, message in cases:
            with self.subTest(kind=kind):
                identifier = self.preflight().json['id']
                page = MagicMock()
                page.is_closed.return_value = False
                page.wait_for_timeout.side_effect = lambda milliseconds: time.sleep(.01)

                def fake_open(*args, **kwargs):
                    getattr(kwargs['web_review'], method)(page, message)
                    self.fail('结束任务后不得继续填写')

                self.open_publish.side_effect = fake_open
                self.start_request(f'/api/listing/{identifier}/start', json={'submit': False})
                paused = self.state(identifier, lambda state: state['phase'] == 'waiting')
                self.assertEqual(paused['prompt']['kind'], kind)
                self.assertIn(message, paused['message'])
                self.assertEqual(self.send(identifier, paused, 'finish').status_code, 200)
                ended = self.state(identifier, lambda state: state['phase'] == 'cancelled')
                self.assertIsNone(ended['prompt'])
                self.assertFalse(ended['submit_attempted'])

    def test_video_review_resumes_or_ends_when_editor_closes(self):
        for close in (False, True):
            with self.subTest(close=close):
                data = self.preflight().json
                page = MagicMock()
                page.is_closed.return_value = False
                page.wait_for_timeout.side_effect = lambda milliseconds: time.sleep(.01)

                def fake_open(*args, **kwargs):
                    review = kwargs['web_review']
                    review.manifest['files']['video'] = ['商品视频.mp4']
                    review.video_review(page)
                    return '已继续填写'

                self.open_publish.side_effect = fake_open
                identifier = data['id']
                self.start_request(f'/api/listing/{identifier}/start', json={'submit': False})
                paused = self.state(identifier, lambda state: state['phase'] == 'waiting')
                self.assertEqual(paused['prompt']['kind'], 'video')
                self.assertIn('商品视频.mp4', paused['message'])
                if close:
                    page.is_closed.return_value = True
                    final = self.state(identifier, lambda state: state['phase'] == 'needs_attention')
                    self.assertIn('千牛页面已关闭', final['message'])
                else:
                    self.assertEqual(self.send(identifier, paused, 'continue').status_code, 200)
                    final = self.state(identifier, lambda state: state['phase'] == 'succeeded')
                    self.assertEqual(final['message'], '已继续填写')
                self.assertIsNone(final['prompt'])
                self.assertFalse(final['submit_attempted'])

    def test_video_ratio_review_offers_three_choices_and_stops(self):
        identifier = self.preflight().json['id']
        page = MagicMock(is_closed=lambda: False)
        page.wait_for_timeout.side_effect = lambda milliseconds: time.sleep(.01)

        def fake_open(*args, **kwargs):
            kwargs['web_review'].video_ratio_review(page, '千牛仍提示视频比例不符')
            self.fail('结束任务后不得继续填写')

        self.open_publish.side_effect = fake_open
        self.start_request(f'/api/listing/{identifier}/start', json={'submit': False})
        paused = self.state(identifier, lambda state: state['phase'] == 'waiting')
        self.assertEqual(paused['prompt']['kind'], 'video_ratio')
        self.assertEqual(paused['prompt']['actions'], ['continue', 'skip_video', 'finish'])
        self.assertEqual(self.send(identifier, paused, 'finish').status_code, 200)
        ended = self.state(identifier, lambda state: state['phase'] == 'cancelled')
        self.assertFalse(ended['submit_attempted'])

    def test_confirmed_receipt_keeps_platform_item_id(self):
        data = self.preflight().json
        def fake_open(*args, **kwargs):
            review = kwargs['web_review']
            review.click_intent()
            review.result(SimpleNamespace(status='succeeded', message='商品已入仓', item_id='123456',
                                          page=MagicMock(is_closed=lambda: True)))
            return '商品已入仓'
        self.open_publish.side_effect = fake_open
        identifier = data['id']
        self.start_request(f'/api/listing/{identifier}/start', json={'submit': True})
        final = self.state(identifier, lambda state: state['phase'] == 'succeeded')
        self.assertEqual((final['outcome'], final['item_id']), ('warehouse', '123456'))

    def test_cross_origin_missing_token_and_unchecked_snapshot_tampering(self):
        self.assertEqual(self.client.post('/api/listing/preflight').status_code, 403)
        self.assertEqual(self.client.post('/api/listing/preflight', headers={**self.headers, 'Origin': 'http://outside.test'}).status_code, 403)
        identifier = self.preflight().json['id']
        path = self.app.extensions['product_listing'].root / identifier / 'main' / '1.png'
        path.write_bytes(b'changed')
        self.start_request(f'/api/listing/{identifier}/start', json={'submit': True})
        finished = self.state(identifier, lambda state: state['phase'] == 'needs_attention')
        self.assertIn('文件发生变化', finished['message'])
        self.open_publish.assert_not_called()
