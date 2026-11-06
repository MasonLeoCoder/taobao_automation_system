"""商品工作台隔离验证：上传、只填、入仓意图和人工恢复。"""
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from openpyxl import Workbook
from PIL import Image
from ecommerce.modules.product.web_materials import CATEGORY
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app


def fixture_settings(root):
    """构造非验证库专用标识的隔离文件后端；不连接MySQL。"""
    settings = load_settings(root=root)
    return replace(settings, environment='', mysql=replace(settings.mysql, database='listing_isolated'))


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
        self.open_publish = self.enterContext(patch('ecommerce.modules.product.web_listing.open_publish'))

    def preflight(self, *, bad_image=False):
        """走真实导入接口；参数：是否破坏图片内容；返回：HTTP响应。"""
        images = [(BytesIO(b'fake' if bad_image and p.stem == '2' else p.read_bytes()), p.name)
                  for p in sorted((self.folder / '1-1主图').iterdir())]
        return self.client.post('/api/listing/preflight', headers=self.headers, data={
            'keyword': '粘毛器', 'brand': '无品牌/无注册商标', 'category': CATEGORY,
            'excel': (BytesIO((self.folder / '任意表名.xlsx').read_bytes()), '任意表名.xlsx'), 'main': images})

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
        for invalid in ({**titles, 'title': '猫' * 31}, {**titles, 'guide': '毛' * 16}):
            response = self.client.post(url, headers=self.headers, json={'submit': False, 'titles': invalid})
            self.assertEqual(response.status_code, 422)
        self.open_publish.assert_not_called()
        self.open_publish.return_value = '填写完成'
        response = self.client.post(url, headers=self.headers, json={'submit': False, 'titles': titles})
        self.assertEqual(response.status_code, 202)
        self.assertEqual({key: response.json['preview'][key] for key in titles}, titles)
        self.state(identifier, lambda state: state['phase'] == 'succeeded')
        self.assertEqual(self.open_publish.call_args.kwargs['title_overrides'], titles)

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
        self.assertEqual(self.client.post(endpoint, headers=self.headers, json={'submit': False}).status_code, 202)
        paused = self.state(identifier, lambda state: state['phase'] == 'waiting')
        repeated = self.client.post(endpoint, headers=self.headers, json={'submit': True}).json
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

    def test_native_prompt_during_login_restores_pending_login(self):
        identifier = self.preflight().json['id']
        native_requested = Event()
        native_answer = []

        def fake_open(*args, **kwargs):
            review = kwargs['web_review']

            def pump(ms):
                if native_requested.wait(ms / 1000) and not native_answer:
                    native_answer.append(review.attention('【需要人工决定原生弹窗】'))

            page = SimpleNamespace(is_closed=lambda: False, wait_for_timeout=pump)
            page.context = SimpleNamespace(pages=[page])
            review.login_wait(page)
            return '登录完成'

        self.open_publish.side_effect = fake_open
        self.client.post(f'/api/listing/{identifier}/start', headers=self.headers, json={'submit': False})
        login = self.state(identifier, lambda state: state['prompt'] and state['prompt']['kind'] == 'login')
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
        actual.update(price='18', sku_media=[])

        def fake_open(*args, **kwargs):
            review = kwargs['web_review']
            review.sku_media = []
            page = SimpleNamespace(is_closed=lambda: False, wait_for_timeout=lambda ms: time.sleep(ms / 1000),
                                   screenshot=lambda **opts: Path(opts['path']).write_bytes(b'fixture'))
            review.before_click(page)
            review.click_intent()
            review.submission_attention('千牛结果未确认')
            return '不可到达'

        self.open_publish.side_effect = fake_open
        with patch('ecommerce.modules.product.web_listing.read_listing_fields', side_effect=lambda page: dict(actual)):
            response = self.client.post(f'/api/listing/{identifier}/start', headers=self.headers, json={'submit': True})
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
        self.client.post(f'/api/listing/{identifier}/start', headers=self.headers, json={'submit': True})
        final = self.state(identifier, lambda state: state['phase'] == 'succeeded')
        self.assertEqual((final['outcome'], final['item_id']), ('warehouse', '123456'))

    def test_cross_origin_missing_token_and_unchecked_snapshot_tampering(self):
        self.assertEqual(self.client.post('/api/listing/preflight').status_code, 403)
        self.assertEqual(self.client.post('/api/listing/preflight', headers={**self.headers, 'Origin': 'http://outside.test'}).status_code, 403)
        identifier = self.preflight().json['id']
        path = self.app.extensions['product_listing'].root / identifier / 'main' / '1.png'
        path.write_bytes(b'changed')
        self.client.post(f'/api/listing/{identifier}/start', headers=self.headers, json={'submit': True})
        finished = self.state(identifier, lambda state: state['phase'] == 'needs_attention')
        self.assertIn('文件发生变化', finished['message'])
        self.open_publish.assert_not_called()
