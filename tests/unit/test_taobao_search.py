"""关键词输入、URL 条件及命令分发回归。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ecommerce.cli.app import parser
from ecommerce.cli.commands import collection
from ecommerce.platforms.taobao.search import canonical_search_url, matches_search_url, validate_keyword
from ecommerce.shared.config import load_settings


class TaobaoSearchTests(unittest.TestCase):
    def test_keyword_validation_keeps_special_characters(self):
        self.assertEqual(validate_keyword("  猫咪 粘毛器 & + / #  "), "猫咪 粘毛器 & + / #")
        for value in (None, "", "   ", "a\nb", "x\x00", "x" * 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_keyword(value)

    def test_encoded_keyword_and_trusted_origin_are_checked(self):
        keyword = "粘毛器 & + #"
        page = MagicMock(url=canonical_search_url(keyword))
        self.assertTrue(matches_search_url(page, keyword))
        for url in ("https://s.taobao.com.evil.example/search?q=x", "http://s.taobao.com/search?q=x",
                    "https://s.taobao.com/search?q=old", "https://s.taobao.com/search?q=x&q=y",
                    "https://s.taobao.com/other?q=x"):
            page.url = url
            self.assertFalse(matches_search_url(page, "x"))

    def test_cli_search_and_existing_control_commands(self):
        arguments = parser().parse_args(["taobao", "search", "--keyword", "粘毛器", "--no-wait"])
        self.assertEqual((arguments.action, arguments.keyword, arguments.no_wait), ("search", "粘毛器", True))
        for action in ("pause", "stop", "continue", "confirm", "cancel"):
            self.assertEqual(parser().parse_args(["taobao", action]).action, action)

    def test_search_uses_taobao_profile_and_shared_task_lock(self):
        with tempfile.TemporaryDirectory() as root:
            settings = load_settings(root=Path(root))
            with patch.object(collection, "run_task", side_effect=lambda settings, name, action: action()) as runner, \
                    patch("ecommerce.modules.collection.service.search_keyword", return_value="done") as search:
                self.assertEqual(collection.execute(settings, " 粘毛器 ", wait=False), "done")
                self.assertEqual(runner.call_args.args[0].profile_path, settings.for_platform("taobao").profile_path)
                self.assertEqual(runner.call_args.args[1], "collection.taobao.search")
                self.assertEqual(search.call_args.args[0].profile_path, settings.for_platform("taobao").profile_path)
                self.assertEqual(search.call_args.args[1], "粘毛器")
                self.assertFalse(search.call_args.kwargs["wait"])


if __name__ == "__main__":
    unittest.main()
