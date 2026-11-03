"""workspace 整理清单的独立只读核验。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "scripts/verification/workspace_audit.py"
SPEC = importlib.util.spec_from_file_location("workspace_audit", MODULE)
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


class WorkspaceAuditTests(unittest.TestCase):
    """不读取正式配置、数据库或真实用户文件。"""

    def setUp(self):
        """准备临时清单；参数：无；返回：无。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "workspace/development/runs/example/results/result.json"
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b'{"ok": true}')
        self.manifest = self.root / "workspace/development/migration-manifest.json"
        self.data = {"files": [{"source": "workspace/result.json", "target": self.target.relative_to(self.root).as_posix(),
                                 "size": self.target.stat().st_size, "sha256": audit_module.digest(self.target), "status": "moved"}]}
        self.save()

    def save(self):
        """保存测试清单；参数：无；返回：无。"""
        self.manifest.write_text(json.dumps(self.data), encoding="utf-8")

    def test_valid_manifest_is_read_only(self):
        """正确归档通过且文件不变；参数：无；返回：无。"""
        before = self.target.read_bytes()
        result = audit_module.audit(self.root, self.manifest)
        self.assertEqual(result["verified_files"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.target.read_bytes(), before)

    def test_tampering_same_size_is_detected(self):
        """相同大小但内容变更仍被发现；参数：无；返回：无。"""
        self.target.write_bytes(b'{"ok": null}')
        self.assertTrue(audit_module.audit(self.root, self.manifest)["errors"])

    def test_missing_file_is_detected(self):
        """缺失文件不得报告成功；参数：无；返回：无。"""
        self.target.unlink()
        self.assertEqual(audit_module.audit(self.root, self.manifest)["verified_files"], 0)

    def test_recreated_source_is_reported(self):
        """旧路径重新生成文件时提示而不删除；参数：无；返回：无。"""
        source = self.root / "workspace/result.json"
        source.write_text("new", encoding="utf-8")
        result = audit_module.audit(self.root, self.manifest)
        self.assertTrue(result["errors"])
        self.assertEqual(source.read_text(encoding="utf-8"), "new")

    def test_escape_is_rejected(self):
        """清单不可读取工作区外的文件；参数：无；返回：无。"""
        self.data["files"][0]["target"] = "../outside"
        self.save()
        self.assertTrue(audit_module.audit(self.root, self.manifest)["errors"])

    def test_incomplete_migration_is_reported(self):
        """计划未完成不能算通过；参数：无；返回：无。"""
        self.data["files"][0]["status"] = "planned"
        self.save()
        self.assertTrue(audit_module.audit(self.root, self.manifest)["errors"])
