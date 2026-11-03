"""环境归属、升级失败保护和重试；所有数据库连接均替换为模拟对象。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from ecommerce.shared.config import load_settings
from ecommerce.infrastructure.storage import environments as env
from ecommerce.infrastructure.storage.mysql import MySQLStorageError


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        """建立三环境的隔离配置；参数：无；返回：无。"""
        directory=TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root=Path(directory.name); (self.root/'config').mkdir()
        (self.root/'config/settings.toml').write_text('[mysql]\ndatabase="taobao_automation"\n',encoding='utf-8')
        base=load_settings(root=self.root)
        self.configs=[replace(base,mysql=replace(base.mysql,database=name)) for name in (
            'taobao_automation_verify','taobao_automation_test','taobao_automation')]
        self.configs[1]=replace(self.configs[1],workspace=self.root/'workspace_test',session_path=self.root/'workspace_test/session.json',profile_path=self.root/'workspace_test/profile')
        self.marker=env.state_path(base.mysql)

    def fake_engine(self, *_args, **_kwargs):
        """返回无需网络的SQL连接；参数：任意；返回：模拟Engine。"""
        engine=MagicMock(); connection=engine.connect.return_value.__enter__.return_value
        connection.scalar.side_effect=lambda sql,*args,**kwargs: 0 if 'COUNT(*)' in str(sql) else 'exists'
        return engine

    def run_upgrade(self, fail=False, different=False, early=False):
        """执行模拟升级；参数：故障及结构差异；返回：维护报告。"""
        from ecommerce.infrastructure.storage import mysql
        reports=[{'revision':'head','schema_errors':[], 'tables':{'products':{'columns':[{'name':'id'}]}}} for _ in range(3)]
        if different: reports[-1]['tables']['products']['columns'].append({'name':'unexpected'})
        with patch.object(env,'registered',return_value=self.configs),patch.object(mysql,'create_mysql_engine',side_effect=self.fake_engine),\
             patch.object(env.sa,'inspect',return_value=MagicMock(has_table=MagicMock(return_value=False))),patch.object(mysql,'current_revision',return_value='old',side_effect=ValueError('synthetic early failure') if early else None),patch.object(mysql,'upgrade_schema',side_effect=RuntimeError('synthetic') if fail else None),\
             patch.object(mysql,'inspect_schema',side_effect=reports):
            return env.upgrade_all(self.root)

    def test_failure_persists_and_successful_retry_releases_gate(self):
        """升级失败跨调用保留禁写，重试全部成功后解除。"""
        with self.assertRaises(MySQLStorageError): self.run_upgrade(fail=True)
        self.assertTrue(self.marker.exists())
        with self.assertRaises(MySQLStorageError): env.assert_writable(self.configs[-1].mysql)
        self.assertEqual(self.run_upgrade()['status'],'succeeded')
        self.assertFalse(self.marker.exists())

    def test_structure_difference_cannot_report_success(self):
        """相同版本号而实际结构不同也必须失败。"""
        with self.assertRaises(MySQLStorageError): self.run_upgrade(different=True)
        self.assertTrue(self.marker.exists())

    def test_test_database_rejects_production_workspace(self):
        """测试库不能绑定正式工作目录。"""
        with self.assertRaises(ValueError): env.validate(self.configs[1])

    def test_explicit_environment_cannot_point_at_another_database(self):
        """标识和库名冲突在启动时拒绝。"""
        path=self.root/'config/settings.toml'
        path.write_text('environment="test"\n[mysql]\ndatabase="taobao_automation"',encoding='utf-8')
        with self.assertRaises(ValueError): load_settings(root=self.root)

    def test_busy_database_does_not_enter_maintenance(self):
        """运行任务在维护前被发现时保留其写入能力。"""
        from ecommerce.infrastructure.storage import mysql
        engine=self.fake_engine()
        engine.connect.return_value.__enter__.return_value.scalar.return_value=1
        engine.connect.return_value.__enter__.return_value.scalar.side_effect=None
        with patch.object(env,'registered',return_value=self.configs),patch.object(mysql,'create_mysql_engine',return_value=engine):
            with self.assertRaises(MySQLStorageError): env.upgrade_all(self.root)
        self.assertFalse(self.marker.exists())

    def test_failed_retry_does_not_erase_existing_maintenance(self):
        """已有失败状态在重试预检查故障时仍须保留。"""
        with self.assertRaises(MySQLStorageError): self.run_upgrade(fail=True)
        original=self.marker.read_bytes()
        with self.assertRaises(MySQLStorageError): self.run_upgrade(early=True)
        self.assertEqual(self.marker.read_bytes(),original)
