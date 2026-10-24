"""已确认会话的临时 Cookie 恢复边界；只使用脱敏样例。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from ecommerce.infrastructure.browser.session import restore_missing_session_cookies
from ecommerce.shared.config import load_settings


class SessionCookieRestoreTests(unittest.TestCase):
    def setUp(self):
        """准备独立临时配置和上下文。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = load_settings(root=Path(self.temp.name)).for_platform("taobao")
        self.context = MagicMock()
        self.context.cookies.return_value = []

    def save(self, cookies, origins=None):
        """保存脱敏状态；参数：Cookie、可选存储；返回：无。"""
        self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.session_path.write_text(json.dumps({"cookies": cookies, "origins": origins or []}), encoding="utf-8")

    def cookie(self, name, **extra):
        """创建样例 Cookie；参数：名称、可选字段；返回：字典。"""
        return {"name": name, "value": "export", "domain": ".local.test", "path": "/", "expires": -1, **extra}

    def test_only_missing_session_cookies_are_restored(self):
        """现有 Cookie 不回退，持久及过期 Cookie 不恢复，localStorage 不回写。"""
        current = self.cookie("current", value="fresh")
        missing = self.cookie("missing")
        self.context.cookies.return_value = [current]
        self.save([self.cookie("current"), missing, self.cookie("persistent", expires=2000000000),
                   self.cookie("expired", expires=1)],
                  [{"origin": "https://local.test", "localStorage": [{"name": "token", "value": "old"}]}])
        self.assertEqual(restore_missing_session_cookies(self.context, self.settings), 1)
        self.context.add_cookies.assert_called_once_with([missing])
        self.context.add_init_script.assert_not_called()

    def test_cookie_path_and_partition_are_independent(self):
        """同名但路径或分区不同的 Cookie 不互相遮蔽。"""
        self.context.cookies.return_value = [self.cookie("login")]
        different_path = self.cookie("login", path="/member")
        partitioned = self.cookie("login", partitionKey="https://other.test")
        self.save([different_path, partitioned])
        self.assertEqual(restore_missing_session_cookies(self.context, self.settings), 2)
        self.context.add_cookies.assert_called_once_with([different_path, partitioned])

    def test_missing_invalid_or_already_present_state_does_not_mutate_context(self):
        """没有可恢复数据或状态损坏时，不注入状态也不伪报恢复。"""
        self.assertEqual(restore_missing_session_cookies(self.context, self.settings), 0)
        self.save([self.cookie("present")])
        self.context.cookies.return_value = [self.cookie("present", value="fresh")]
        self.assertEqual(restore_missing_session_cookies(self.context, self.settings), 0)
        for value in ('{broken', '[]', '{"cookies": [null]}', '{"cookies": [{}]}'):
            self.settings.session_path.write_text(value, encoding="utf-8")
            with self.subTest(state=value):
                self.assertEqual(restore_missing_session_cookies(self.context, self.settings), 0)
        self.context.add_cookies.assert_not_called()
        self.context.add_init_script.assert_not_called()


if __name__ == "__main__":
    unittest.main()
