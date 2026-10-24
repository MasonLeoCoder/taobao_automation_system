"""精简模型、命令边界和双平台升级锁的离线验证。"""
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from ecommerce.cli.app import parser
from ecommerce.infrastructure.storage.mysql_migrations import schema_v4, schema_v5
from ecommerce.infrastructure.tasks.lock import all_browser_resources, shop_lock
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import TaskBusy


class RemoveHistoryImportTests(unittest.TestCase):
    def test_only_import_tables_columns_and_foreign_keys_are_removed(self):
        """新版仅删除约定结构，旧模型每次构造仍保持原样。"""
        old=schema_v4.build_metadata()
        new=schema_v5.build_metadata()
        self.assertEqual(set(old.tables)-set(new.tables),set(schema_v5.IMPORT_TABLES))
        for name,table in new.tables.items():
            expected=set(old.tables[name].c)-{old.tables[name].c.import_run_id} if name in schema_v5.OWNER_TABLES else set(old.tables[name].c)
            self.assertEqual(set(table.c.keys()),{c.name for c in expected})
            self.assertEqual(table.comment,old.tables[name].comment)
            expected_fk={fk.name for fk in old.tables[name].foreign_key_constraints if 'import_run_id' not in fk.column_keys}
            self.assertEqual({fk.name for fk in table.foreign_key_constraints},expected_fk)
            self.assertTrue(all(c.comment for c in table.c))
        self.assertEqual(len(schema_v4.build_metadata().tables),22)
        self.assertEqual((len(new.tables),sum(len(t.c) for t in new.tables.values())),(18,244))

    def test_import_commands_retired_but_replay_and_database_tools_remain(self):
        """命令帮助不再提供历史导入，补写、升级、核对和字典仍可解析。"""
        for action in ('import-task-history','import-collection-history'):
            with redirect_stderr(StringIO()),self.assertRaises(SystemExit):
                parser().parse_args(['database',action])
        for action in ('replay-events','upgrade','check','dictionary','verify'):
            self.assertEqual(parser().parse_args(['database',action]).action,action)

    def test_upgrade_locks_both_platforms_and_releases_partial_locks(self):
        """升级入口锁定两平台，失败时不遗留已取得资源锁。"""
        with tempfile.TemporaryDirectory() as folder:
            settings=load_settings(root=Path(folder))
            for platform in ('qianniu','taobao'):
                with shop_lock(settings.for_platform(platform).profile_path),self.assertRaises(TaskBusy):
                    with all_browser_resources(settings):
                        self.fail('资源忙时不得升级')
            with all_browser_resources(settings):
                pass
