"""详情图数量与数字顺序检查，不操作店铺。"""
import tempfile
import unittest
from pathlib import Path
from ecommerce.modules.product.assets import load_detail_images


class DetailImageTests(unittest.TestCase):
    def test_all_images_in_numeric_order(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            for number in reversed(range(1, 23)):
                (folder / f"{number}.jpg").write_bytes(b"image")
            (folder / "notes.txt").write_text("ignore", encoding="utf-8")
            self.assertEqual([p.stem for p in load_detail_images(folder)],
                             [str(i) for i in range(1, 23)])

    def test_single_image_and_gaps_are_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / "3.png").write_bytes(b"image")
            self.assertEqual(len(load_detail_images(folder)), 1)
            (folder / "12.jpg").write_bytes(b"image")
            self.assertEqual([p.stem for p in load_detail_images(folder)], ["3", "12"])

    def test_invalid_files_fail_before_browser(self):
        for names in ([], ["a.jpg"], ["0.jpg"], ["1.jpg", "01.png"]):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as root:
                folder = Path(root)
                for name in names:
                    (folder / name).write_bytes(b"image")
                with self.assertRaises(ValueError):
                    load_detail_images(folder)
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "1.jpg").touch()
            with self.assertRaises(ValueError):
                load_detail_images(Path(root))

    def test_long_image_segments_preserve_all_pixels_in_order(self):
        from ecommerce.platforms.qianniu.product_details import expected_segments
        source = "https://img.alicdn.com/imgextra/i2/shop/image_!!shop.jpg"
        self.assertEqual(expected_segments(source, 800, 1911), [
            "https://img.alicdn.com/imgextra/i2/shop/image~crop,0,0,800,1600~_!!shop.jpg",
            "https://img.alicdn.com/imgextra/i2/shop/image~crop,0,1600,800,311~_!!shop.jpg"])
        self.assertEqual(expected_segments(source, 800, 1600), [source])
        self.assertEqual(len(expected_segments(source, 800, 4801)), 4)

    def test_cli_accepts_details_directory(self):
        from ecommerce.cli.app import parser
        args = parser().parse_args(["product", "open", "--detail-images-dir", "images"])
        self.assertEqual(args.detail_images_dir, "images")

    def test_invalid_details_prevent_browser_start(self):
        from unittest.mock import patch
        from ecommerce.modules.product import service
        from ecommerce.shared.config import load_settings
        with tempfile.TemporaryDirectory() as root, patch.object(service, "browser_session") as browser:
            with self.assertRaises(ValueError):
                service.open_publish(load_settings(root=Path(root)), keyword="测试", category="一级 > 二级",
                                     brand="无品牌", detail_images_dir="missing")
            browser.assert_not_called()

    def test_details_run_on_editor_before_ready(self):
        from contextlib import ExitStack
        from unittest.mock import MagicMock, patch
        from ecommerce.modules.product import service
        from ecommerce.shared.config import load_settings
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            folder = Path(root) / "details"
            folder.mkdir()
            (folder / "1.jpg").write_bytes(b"image")
            initial, editor = MagicMock(), MagicMock()
            browser = stack.enter_context(patch.object(service, "browser_session"))
            browser.return_value.__enter__.return_value = (MagicMock(), initial)
            for name in ("authenticate", "click_product_nav", "click_publish_product"):
                stack.enter_context(patch.object(service, name, return_value=initial))
            for name in ("search_publish_products", "select_publish_category"):
                stack.enter_context(patch.object(service, name))
            stack.enter_context(patch.object(service, "select_brand_and_continue", return_value=editor))
            calls = []
            upload = stack.enter_context(patch.object(service, "upload_detail_images", side_effect=lambda *args: calls.append("details")))
            result = service.open_publish(load_settings(root=Path(root)), keyword="测试", category="一级 > 二级",
                                          brand="无品牌", detail_images_dir="details", on_ready=lambda: calls.append("ready"))
            upload.assert_called_once_with(editor, [folder / "1.jpg"])
            self.assertEqual(calls, ["details", "ready"])
            self.assertIn("1张详情图", result)
            ready = MagicMock()
            upload.side_effect = RuntimeError("上传未完成")
            with self.assertRaisesRegex(RuntimeError, "上传未完成"):
                service.open_publish(load_settings(root=Path(root)), keyword="测试", category="一级 > 二级",
                                     brand="无品牌", detail_images_dir="details", on_ready=ready)
            ready.assert_not_called()
