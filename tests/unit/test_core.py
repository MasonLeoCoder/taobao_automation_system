"""验证路径兼容、数据库升级及跨进程任务互斥。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.infrastructure.tasks.runner import run_task


class CoreTests(unittest.TestCase):
    """在临时目录验证，不读取真实登录资料。"""

    def setUp(self):
        """建立临时配置；参数：无；返回：无。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = load_settings(root=self.root)

    def test_legacy_paths_and_explicit_override(self):
        """验证旧资料优先及配置覆盖；参数：无；返回：无。"""
        (self.root / "state.json").write_text("{}")
        (self.root / "storage/taobao_browser_profile").mkdir(parents=True)
        legacy = load_settings(root=self.root)
        self.assertEqual(legacy.session_path, self.root / "state.json")
        (self.root / "config").mkdir()
        (self.root / "config/settings.toml").write_text('[browser]\nsession_path="new/state.json"\nprofile_path="new/profile"')
        explicit = load_settings(root=self.root)
        self.assertEqual(explicit.session_path, self.root / "new/state.json")
        self.assertEqual(explicit.profile_path, self.root / "new/profile")

    def test_bad_config_fails(self):
        """错误配置不能被静默忽略；参数：无；返回：无。"""
        with self.assertRaises(ValueError):
            load_settings("missing.toml", root=self.root)
        (self.root / "bad.toml").write_text('[web]\nhost="0.0.0.0"')
        with self.assertRaises(ValueError):
            load_settings("bad.toml", root=self.root)

    def test_migrations_preserve_history(self):
        """重复初始化保留数据并拒绝未知新版本；参数：无；返回：无。"""
        path = self.settings.database
        database.initialize(path)
        task = database.create_task(path, "example")
        database.finish_task(path, task, "succeeded", "ok")
        database.initialize(path)
        self.assertEqual(database.list_tasks(path)[0]["message"], "ok")
        with database.connect(path) as connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaises(RuntimeError):
            database.initialize(path)

    def test_task_results(self):
        """成功、失败、人工处理及取消均准确落库；参数：无；返回：无。"""
        self.assertEqual(run_task(self.settings, "success", lambda: "done").exit_code, 0)

        def fail(error):
            """抛出测试异常；参数：异常；返回：无。"""
            raise error

        for error, status, code in [(RuntimeError("broken"), "failed", 1), (NeedsAttention("scan"), "needs_attention", 3), (KeyboardInterrupt(), "cancelled", 130)]:
            result = run_task(self.settings, status, lambda: fail(error))
            self.assertEqual((result.status, result.exit_code), (status, code))
        records = database.list_tasks(self.settings.database)
        self.assertEqual(len(records), 4)
        self.assertTrue(all(row["finished_at"] for row in records))

    def test_stale_task_recovered_only_under_lock(self):
        """新任务取得锁后才处理遗留记录；参数：无；返回：无。"""
        database.initialize(self.settings.database)
        stale = database.create_task(self.settings.database, "interrupted")
        with shop_lock(self.settings.profile_path):
            result = run_task(self.settings, "blocked", lambda: self.fail("不得执行"))
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(database.list_tasks(self.settings.database)[0]["status"], "running")
        run_task(self.settings, "next", lambda: "ok")
        record = next(row for row in database.list_tasks(self.settings.database) if row["id"] == stale)
        self.assertEqual(record["status"], "needs_attention")

    def test_cross_process_lock_and_release(self):
        """真实子进程不能越过锁，释放后可执行；参数：无；返回：无。"""
        code = '''from pathlib import Path
import sys
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.shared.exceptions import TaskBusy
try:
    with shop_lock(Path(sys.argv[1])): pass
except TaskBusy:
    sys.exit(3)
'''
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
        with shop_lock(self.settings.profile_path):
            blocked = subprocess.run([sys.executable, "-c", code, str(self.settings.profile_path)], env=env, capture_output=True)
            self.assertEqual(blocked.returncode, 3, blocked.stderr)
        released = subprocess.run([sys.executable, "-c", code, str(self.settings.profile_path)], env=env, capture_output=True)
        self.assertEqual(released.returncode, 0, released.stderr)
