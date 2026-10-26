"""列表图片下载与快照关联的离线验证。"""
import io
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch


from ecommerce.modules.collection.list_images import collect_list_image, collect_list_images
from ecommerce.modules.collection.database_records import batch_rows
from ecommerce.infrastructure.storage.collection_snapshot import file_record
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import TaskStopped


class Response(io.BytesIO):
    """返回受控图片响应，不访问网络。"""
    def __init__(self, data, content_type='image/png', length=None):
        super().__init__(data)
        self.status = 200
        self.url = 'https://img.alicdn.com/sample.png'
        self.headers = Message()
        self.headers['Content-Type'] = content_type
        self.headers['Content-Length'] = str(len(data) if length is None else length)


class ListImageTests(unittest.TestCase):
    def setUp(self):
        """准备临时目录和受控浏览器接口；参数：无；返回：无。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = load_settings(root=Path(temporary.name))
        self.folder = self.settings.workspace / 'output/images'
        self.context = Mock()
        self.context.cookies.return_value = []
        self.page = Mock()
        self.page.evaluate.side_effect = lambda script, *args: {'width': 1, 'height': 1} if args else 'test-agent'
        self.url = 'https://img.alicdn.com/sample.png'

    def test_download_validation_dedup_and_no_partial_file(self):
        """有效响应落地；同内容不覆盖；截断、HTML 和非允许域不能成为图片。"""
        data = b'\x89PNG\r\n\x1a\nfixture'
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: Response(data)
        with patch('ecommerce.modules.collection.list_images.build_opener', return_value=opener):
            first = collect_list_image(self.context, self.page, self.url, self.folder, lambda step: None)
            second = collect_list_image(self.context, self.page, self.url, self.folder, lambda step: None)
        self.assertEqual(first['status'], 'downloaded')
        self.assertEqual(first['image_path'], second['image_path'])
        self.assertEqual(len(list(self.folder.iterdir())), 1)
        for response in (lambda: Response(b'<html>error</html>', 'text/html'), lambda: Response(data, length=100)):
            opener.open.side_effect = lambda *args, **kwargs: response()
            with patch('ecommerce.modules.collection.list_images.build_opener', return_value=opener):
                self.assertEqual(collect_list_image(self.context, self.page, self.url, self.folder, lambda step: None)['status'], 'failed')
        self.assertFalse(list(self.folder.glob('*.part')))
        self.assertEqual(collect_list_image(self.context, self.page, 'http://127.0.0.1/x', self.folder, lambda step: None)['status'], 'failed')

    def test_failure_retry_missing_address_and_stop(self):
        """下载最多尝试三次；无链接不请求；停止信号不得当成图片失败吞掉。"""
        with patch('ecommerce.modules.collection.list_images.download_list_image', side_effect=TimeoutError) as download:
            result = collect_list_image(self.context, self.page, self.url, self.folder, lambda step: None)
            self.assertEqual((result['status'], download.call_count), ('failed', 3))
            result = collect_list_image(self.context, self.page, None, self.folder, lambda step: None)
            self.assertEqual((result['status'], download.call_count), ('not_applicable', 3))
        with self.assertRaises(TaskStopped):
            collect_list_image(self.context, self.page, self.url, self.folder, Mock(side_effect=TaskStopped('stop')))

    def test_page_failures_keep_items_and_snapshot_positions(self):
        """图片失败仍保留商品；图片文件按原格子位置入快照。"""
        self.folder.mkdir(parents=True)
        path = self.folder / 'a.png'
        path.write_bytes(b'fixture')
        saved = {'status': 'downloaded', 'image_path': str(path), 'sha256': file_record(self.settings.workspace, path)['sha256'], 'size_bytes': 7}
        items = [{'position': 2, 'product_index': 1, 'title': 'A', 'main_image_url': self.url},
                 {'position': 5, 'product_index': 2, 'title': 'B'}]
        with patch('ecommerce.modules.collection.list_images.collect_list_image', side_effect=[saved, {'status': 'not_applicable', 'reason': '未获取图片地址'}]):
            counts = collect_list_images(self.settings, self.context, self.page, items, 'a'*32, lambda step: None)
        self.assertEqual((len(items), counts['downloaded'], counts['not_applicable']), (2, 1, 1))
        rows = list(batch_rows(self.settings, 'list', {'items': items}, self.folder))
        images = [row for row in rows if row['table'] == 'media_references']
        self.assertEqual([r['values']['position'] for r in images], [2, 5])
        self.assertEqual(images[0]['file']['availability'], 'present')
        self.assertIsNone(images[1]['file'])

