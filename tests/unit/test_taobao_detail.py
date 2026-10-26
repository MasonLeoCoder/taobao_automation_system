"""详情输入分流、目标 URL 核对及独立入口调度。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ecommerce.cli.app import main, parser
from ecommerce.cli.commands import collection
from ecommerce.infrastructure.tasks.runner import TaskResult
from ecommerce.platforms.taobao.detail import matches_detail_url, resolve_detail_input
from ecommerce.platforms.taobao.detail_review_summary import parse_display_count, parse_positive_rate
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

    def test_cli_default_collects_all_steps_for_id_or_url_with_optional_modifiers(self):
        for value in ("621242715247", "https://item.taobao.com/item.htm?id=621242715247"):
            for flags in ([], ["--no-wait"], ["--all"], ["--all", "--no-wait"]):
                with self.subTest(value=value, flags=flags), patch("ecommerce.cli.app.load_settings") as settings, \
                        patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                        patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
                    self.assertEqual(main(["taobao", "detail", "--input", value, *flags]), 0)
                    opened.assert_called_once_with(settings.return_value, value, wait="--no-wait" not in flags,
                        basic_info=True, video=True, main_images=True, skus=True, parameters=True, detail_images=True,
                        review_summary=True, reviews=True, all_reviews="--all" in flags, questions=True)

    def test_cli_video_step_is_explicitly_enabled(self):
        with patch("ecommerce.cli.app.load_settings") as settings, \
                patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
            self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", "--video"]), 0)
            opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, basic_info=False, video=True, main_images=False, skus=False, parameters=False, detail_images=False, review_summary=False, reviews=False, all_reviews=False, questions=False)

    def test_cli_main_images_can_run_without_video(self):
        with patch("ecommerce.cli.app.load_settings") as settings, \
                patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
            self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", "--main-images"]), 0)
            opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, basic_info=False, video=False, main_images=True, skus=False, parameters=False, detail_images=False, review_summary=False, reviews=False, all_reviews=False, questions=False)

    def test_cli_skus_can_run_independently(self):
        with patch("ecommerce.cli.app.load_settings") as settings, \
                patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
            self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", "--skus"]), 0)
            opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, basic_info=False, video=False, main_images=False, skus=True, parameters=False, detail_images=False, review_summary=False, reviews=False, all_reviews=False, questions=False)

    def test_cli_parameters_run_alone_or_with_other_detail_steps(self):
        for flags in (["--parameters"], ["--video", "--main-images", "--skus", "--parameters"]):
            with self.subTest(flags=flags), patch("ecommerce.cli.app.load_settings") as settings, \
                    patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                    patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
                self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", *flags]), 0)
                opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, basic_info=False,
                                              video="--video" in flags, main_images="--main-images" in flags,
                                              skus="--skus" in flags, parameters=True, detail_images=False, review_summary=False, reviews=False, all_reviews=False, questions=False)

    def test_cli_detail_images_run_alone_or_with_all_existing_steps(self):
        for flags in (["--detail-images"], ["--video", "--main-images", "--skus", "--parameters", "--detail-images"]):
            with self.subTest(flags=flags), patch("ecommerce.cli.app.load_settings") as settings, \
                    patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                    patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
                self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", *flags]), 0)
                opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, basic_info=False,
                    video="--video" in flags, main_images="--main-images" in flags, skus="--skus" in flags,
                    parameters="--parameters" in flags, detail_images=True, review_summary=False, reviews=False, all_reviews=False, questions=False)


    def test_cli_review_summary_runs_alone_or_with_previous_steps(self):
        for flags in (["--review-summary"], ["--video", "--main-images", "--skus", "--parameters", "--detail-images", "--review-summary"]):
            with self.subTest(flags=flags), patch("ecommerce.cli.app.load_settings") as settings, \
                    patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                    patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
                self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", *flags]), 0)
                opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, basic_info=False,
                    video="--video" in flags, main_images="--main-images" in flags, skus="--skus" in flags,
                    parameters="--parameters" in flags, detail_images="--detail-images" in flags, review_summary=True, reviews=False, all_reviews=False, questions=False)

    def test_cli_reviews_defaults_and_all_mode(self):
        for all_mode in (False, True):
            flags = ["--reviews", *(["--all"] if all_mode else [])]
            with self.subTest(all_mode=all_mode), patch("ecommerce.cli.app.load_settings") as settings, \
                    patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                    patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
                self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", *flags]), 0)
                opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, basic_info=False, video=False,
                    main_images=False, skus=False, parameters=False, detail_images=False, review_summary=False, reviews=True, all_reviews=all_mode, questions=False)

    def test_cli_questions_and_shared_all_mode(self):
        for flags in (["--questions"], ["--questions", "--all"], ["--reviews", "--questions", "--all"]):
            with self.subTest(flags=flags), patch("ecommerce.cli.app.load_settings") as settings, \
                    patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                    patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
                self.assertEqual(main(["taobao", "detail", "--input", "1050158363561", *flags]), 0)
                opened.assert_called_once_with(settings.return_value, "1050158363561", wait=True, basic_info=False,
                    video=False, main_images=False, skus=False, parameters=False, detail_images=False,
                    review_summary=False, reviews="--reviews" in flags, all_reviews="--all" in flags, questions=True)

    def test_cli_explicit_video_and_parameters_do_not_enable_other_steps(self):
        with patch("ecommerce.cli.app.load_settings") as settings, \
                patch("ecommerce.cli.app.prepare_workspace"), patch("ecommerce.cli.app.setup_logging"), \
                patch.object(collection, "execute_detail", return_value=TaskResult("test", "succeeded", "done", 0)) as opened:
            self.assertEqual(main(["taobao", "detail", "--input", "621242715247", "--video", "--parameters", "--no-wait"]), 0)
            opened.assert_called_once_with(settings.return_value, "621242715247", wait=False, basic_info=False,
                video=True, main_images=False, skus=False, parameters=True, detail_images=False,
                review_summary=False, reviews=False, all_reviews=False, questions=False)

    def test_all_requires_reviews_or_questions_before_loading_settings_or_browser(self):
        with patch("ecommerce.cli.app.load_settings") as settings, self.assertRaises(SystemExit) as error:
            main(["taobao", "detail", "--input", "1050158363561", "--skus", "--all"])
        self.assertEqual(error.exception.code, 2)
        settings.assert_not_called()
        with patch.object(collection, "run_task") as task, self.assertRaises(ValueError):
            collection.execute_detail(None, "1050158363561", all_reviews=True)
        task.assert_not_called()

    def test_review_counts_preserve_approximate_and_unknown_values(self):
        for raw, status, exact, lower in [('10万+', 'lower_bound', None, 100000), ('1.2万＋', 'lower_bound', None, 12000),
                ('10万', 'approximate', None, None), ('1亿', 'approximate', None, None),
                ('315', 'exact', 315, None), ('10,123', 'exact', 10123, None), ('0', 'exact', 0, None),
                (None, 'not_displayed', None, None), ('暂无', 'unparsed', None, None)]:
            with self.subTest(raw=raw):
                value = parse_display_count(raw)
                self.assertEqual((value['raw'], value['status'], value['value'], value['lower_bound']), (raw, status, exact, lower))
        self.assertFalse(parse_display_count('10万')['is_exact'])

    def test_review_rate_retains_period_and_does_not_invent_one(self):
        self.assertEqual(parse_positive_rate('近3个月好评率高达99.5%')['period_raw'], '近3个月')
        self.assertEqual(parse_positive_rate('近3个月好评率高达99.5%')['percent'], 99.5)
        self.assertIsNone(parse_positive_rate('好评率100%')['period_raw'])
        self.assertEqual(parse_positive_rate('好评率101%')['status'], 'unparsed')
        self.assertEqual(parse_positive_rate(None)['status'], 'not_displayed')


if __name__ == "__main__":
    unittest.main()
