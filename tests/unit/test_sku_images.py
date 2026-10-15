"""SKU图片按完整文件名匹配，缺失、歧义或不可用素材只跳过对应SKU。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ecommerce.modules.product.assets import load_sku_images
from ecommerce.modules.product.service import open_publish
from ecommerce.shared.config import load_settings
from ecommerce.cli.app import parser


class SkuImageTests(unittest.TestCase):
    def test_exact_unicode_names_and_varied_extensions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            names = ['规格【加长】61cm', '规格 2.0', '自定义格式']
            paths = [root/(name+suffix) for name,suffix in zip(names,['.PNG','.WeBp','.heic'])]
            for file in paths:
                file.write_bytes(b'image')
            (root/'规格【加长】.jpg').write_bytes(b'other-image')
            result = load_sku_images(root, names)
            self.assertEqual(result.files, dict(zip(names,paths)))
            self.assertEqual(result.missing, {})

    def test_no_fuzzy_match_ambiguous_extensions_empty_and_large_files_skip_with_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('歧义.png','歧义.jpg','只匹配部分名称.png'):
                (root/name).write_bytes(b'image')
            (root/'空图片.gif').touch()
            with (root/'超大图片.bmp').open('wb') as stream:
                stream.truncate(20*1024*1024+1)
            names = ['歧义','空图片','超大图片','部分名称']
            with self.assertLogs('ecommerce.modules.product.assets', level='WARNING') as logs:
                result = load_sku_images(root,names)
            self.assertEqual(result.files,{})
            self.assertIn('不唯一',result.missing['歧义'])
            self.assertIn('为空',result.missing['空图片'])
            self.assertIn('20MB',result.missing['超大图片'])
            self.assertIn('未找到图片',result.missing['部分名称'])
            self.assertEqual(len(logs.output),4)

    def test_missing_directory_is_not_fatal_and_permission_failure_has_precise_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with self.assertLogs('ecommerce.modules.product.assets',level='WARNING'):
                result=load_sku_images(root/'不存在',['规格'])
            self.assertIn('目录不存在',result.missing['规格'])
            with patch.object(Path,'iterdir',side_effect=PermissionError('拒绝访问')), self.assertLogs('ecommerce.modules.product.assets',level='WARNING'):
                result=load_sku_images(root,['规格'])
            self.assertIn('拒绝访问',result.missing['规格'])

    def test_new_flag_requires_excel_before_starting_browser(self):
        args=parser().parse_args(['product','open','--sku-images-dir','sku图'])
        self.assertEqual(args.sku_images_dir,'sku图')
        with tempfile.TemporaryDirectory() as folder, patch('ecommerce.modules.product.service.browser_session') as browser:
            with self.assertRaisesRegex(ValueError,'商品Excel'):
                open_publish(load_settings(root=Path(folder)),sku_images_dir='sku图')
            browser.assert_not_called()
