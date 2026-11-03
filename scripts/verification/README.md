# 可复用的只读验证工具

## workspace 迁移核验

```cmd
.venv\Scripts\python.exe -B scripts\verification\workspace_audit.py
```

校验本机迁移清单中的状态、原位置、目标文件大小和 SHA256；不调用 AI、数据库、网络或历史脚本。
完整用法与归档说明见 [workspace 文件整理](../../docs/操作参考/workspace文件整理.md)。

一次性脚本保存在 `workspace/development/runs` 或 `archive`，不自动纳入工具目录。把脚本提升为可复用工具前，应明确参数、输入输出、副作用和测试，移除固定业务编号及敏感配置。
