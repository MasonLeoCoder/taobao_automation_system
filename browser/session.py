"""浏览器执行层：管理 Playwright 登录状态文件。"""

import json
from pathlib import Path


def has_valid_storage_state(storage_state_path: Path) -> bool:
    """检查 Playwright 登录状态文件是否存在且格式有效。

    参数：
        storage_state_path: 登录状态 JSON 文件路径。

    返回：
        如果文件存在且是合法 JSON，返回 True；否则返回 False。
    """
    if not storage_state_path.exists():
        return False

    try:
        json.loads(storage_state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False

    return True
