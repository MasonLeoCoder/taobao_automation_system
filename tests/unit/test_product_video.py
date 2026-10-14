"""视频输入与流程接入回归，不访问真实店铺。"""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch
from ecommerce.modules.product.assets import load_product_video
from ecommerce.modules.product import service
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.platforms.qianniu import product_video


class ProductVideoTests(unittest.TestCase):
    def test_file_validation(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "test.mp4"
            with self.assertRaises(ValueError): load_product_video(file)
            file.touch()
            with self.assertRaises(ValueError): load_product_video(file)
            file.write_bytes(b"video")
            self.assertEqual(load_product_video(file), file.resolve())
            other = file.with_suffix(".jpg")
            other.write_bytes(b"video")
            with self.assertRaises(ValueError): load_product_video(other)

    def test_bad_path_never_starts_browser(self):
        with tempfile.TemporaryDirectory() as root, patch.object(service, "browser_session") as browser:
            with self.assertRaises(ValueError):
                service.open_publish(load_settings(root=Path(root)), keyword="测试", category="一 > 二",
                                     brand="无品牌", video_file="missing.mp4")
            browser.assert_not_called()

    def test_existing_video_is_not_overwritten(self):
        page = MagicMock()
        page.locator.return_value.is_visible.return_value = True
        page.locator.return_value.locator.return_value.count.return_value = 1
        with patch.object(product_video, "expect"):
            with self.assertRaises(NeedsAttention):
                product_video._open_picker(page)
        page.frame_locator.assert_not_called()

    def test_video_on_editor_precedes_ready_and_errors_stop_ready(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            file = Path(root) / "video.mp4"
            file.write_bytes(b"video")
            initial, editor = MagicMock(), MagicMock()
            browser = stack.enter_context(patch.object(service, "browser_session"))
            browser.return_value.__enter__.return_value = (MagicMock(), initial)
            for name in ("authenticate", "click_product_nav", "click_publish_product"):
                stack.enter_context(patch.object(service, name, return_value=initial))
            for name in ("search_publish_products", "select_publish_category"):
                stack.enter_context(patch.object(service, name))
            stack.enter_context(patch.object(service, "select_brand_and_continue", return_value=editor))
            calls = []
            upload = stack.enter_context(patch.object(product_video, "upload_product_video", side_effect=lambda *a: calls.append("video")))
            settings = load_settings(root=Path(root))
            result = service.open_publish(settings, keyword="测试", category="一 > 二", brand="无品牌", video_file="video.mp4", on_ready=lambda: calls.append("ready"))
            upload.assert_called_once_with(editor, file)
            self.assertEqual(calls, ["video", "ready"])
            self.assertIn("商品视频上传", result)
            ready = MagicMock()
            upload.side_effect = RuntimeError("视频未处理完")
            with self.assertRaises(RuntimeError):
                service.open_publish(settings, keyword="测试", category="一 > 二", brand="无品牌", video_file="video.mp4", on_ready=ready)
            ready.assert_not_called()

    def test_cli_video_path(self):
        from ecommerce.cli.app import parser
        args = parser().parse_args(["product", "open", "--video-file", "a.mp4"])
        self.assertEqual(args.video_file, "a.mp4")
