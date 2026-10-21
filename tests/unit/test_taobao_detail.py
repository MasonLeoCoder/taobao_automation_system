"""详情输入分流、目标 URL 核对及独立入口调度。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ecommerce.cli.app import main, parser
from ecommerce.cli.commands import collection
from ecommerce.infrastructure.tasks.runner import TaskResult
from ecommerce.platforms.taobao.detail import matches_detail_url, resolve_detail_input
from ecommerce.shared.config import load_settings


class TaobaoDetailTests(unittest.TestCase):
    def test_numeric_id_uses_fixed_taobao_url(self):
        target = resolve_detail_input(" 1007759256325 ")
        self.assertEqual(target.item_id, "1007759256325")
        self.assertEqual(target.input_type, "id")
        self.assertEqual(target.url, "https://item.taobao.com/item.htm?id=1007759256325")

    def test_detail_url_is_preserved_but_record_url_drops_tracking(self):
        for host in ("item.taobao.com", "detail.tmall.com"):
            url = f"https://{host}/item.htm?spm=keep-on-navigation&id=1007759256325&skuId=6001949700618#sku"
            with self.subTest(host=host):
                target = resolve_detail_input(url)
                self.assertEqual(target.url, url)
                self.assertEqual(target.input_type, "url")
                self.assertEqual(target.item_id, "1007759256325")
                self.assertEqual(target.canonical_url, f"https://{host}/item.htm?id=1007759256325")

    def test_invalid_or_ambiguous_inputs_are_rejected_before_browser_launch(self):
        for value in (None, "", "0", "-1", "1.5", "１２３４５", "1" * 26, "123\n456",
                      "https://item.taobao.com/item.htm", "https://item.taobao.com/item.htm?id=0",
                      "https://item.taobao.com/item.htm?id=123456&id=234567",
                      "https://item.taobao.com/item.htm?id=",
                      "https://item.taobao.com.evil.example/item.htm?id=123456",
                      "https://user:password@item.taobao.com/item.htm?id=123456",
                      "https://item.taobao.com:8080/item.htm?id=123456",
                      "https://item.taobao.com:bad/item.htm?id=123456",
                      "https://s.taobao.com/search?q=123456", "javascript:alert(1)"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_detail_input(value)
        with tempfile.TemporaryDirectory() as folder, patch.object(collection, "run_task") as runner:
            with self.assertRaises(ValueError):
                collection.execute_detail(load_settings(root=Path(folder)), "not-an-item")
            runner.assert_not_called()

    def test_only_requested_id_on_supported_detail_pages_matches(self):
        page = MagicMock(url="https://detail.tmall.com/item.htm?id=1007759256325")
        page.is_closed.return_value = False
        self.assertTrue(matches_detail_url(page, "1007759256325"))
        for url in ("https://item.taobao.com/item.htm?id=1050158363561",
                    "https://item.taobao.com/item.htm?id=1007759256325&id=1007759256325",
                    "https://example.invalid/item.htm?id=1007759256325", "about:blank"):
            page.url = url
            self.assertFalse(matches_detail_url(page, "1007759256325"))

    def test_independent_detail_entry_uses_taobao_profile_and_shared_task_lock(self):
        args = parser().parse_args(["taobao", "detail", "--input", "1007759256325", "--no-wait"])
        self.assertEqual((args.action, args.input, args.no_wait), ("detail", "1007759256325", True))
        with tempfile.TemporaryDirectory() as folder:
            settings = load_settings(root=Path(folder))
            with patch.object(collection, "run_task", side_effect=lambda settings, name, action: action()) as runner, \
                    patch("ecommerce.modules.collection.detail_service.open_detail_input", return_value="done") as opened:
                self.assertEqual(collection.execute_detail(settings, args.input, wait=False), "done")
                self.assertIs(runner.call_args.args[0], settings)
                self.assertEqual(runner.call_args.args[1], "collection.taobao.detail.open")
                self.assertEqual(opened.call_args.args[0].profile_path, settings.for_platform("taobao").profile_path)
                self.assertFalse(opened.call_args.kwargs["wait"])

    def test_cli_main_routes_detail_without_search_arguments(self):
        with patch("ecommerce.cli.app.load_settings") as settings, \
                patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
            self.assertEqual(main(["taobao", "detail", "--input", "1007759256325", "--no-wait"]), 0)
            opened.assert_called_once_with(settings.return_value, "1007759256325", wait=False, video=False, main_images=False, skus=False)

    def test_cli_video_step_is_explicitly_enabled(self):
        with patch("ecommerce.cli.app.load_settings") as settings, \
                patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
            self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", "--video"]), 0)
            opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, video=True, main_images=False, skus=False)

    def test_cli_main_images_can_run_without_video(self):
        with patch("ecommerce.cli.app.load_settings") as settings, \
                patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
            self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", "--main-images"]), 0)
            opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, video=False, main_images=True, skus=False)

    def test_cli_skus_can_run_independently(self):
        with patch("ecommerce.cli.app.load_settings") as settings, \
                patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
            self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", "--skus"]), 0)
            opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, video=False, main_images=False, skus=True)


if __name__ == "__main__":
    unittest.main()
