"""数据层：管理输出文件命名和保存路径。"""

from datetime import date
from pathlib import Path

from config.settings import OUTPUT_DIR


def build_output_path(task_name: str, suffix: str = ".xlsx") -> Path:
    """生成按日期命名的输出路径，并避免覆盖历史数据。

    参数：
        task_name: 业务任务名称，用于组成输出文件名。
        suffix: 输出文件扩展名。

    返回：
        输出文件的完整路径。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{date.today().isoformat()}_{task_name}{suffix}"
    output_path = OUTPUT_DIR / filename

    if output_path.exists():
        raise FileExistsError(f"Output file already exists: {output_path}")

    return output_path
