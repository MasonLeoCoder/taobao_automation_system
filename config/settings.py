"""项目配置：统一管理路径、重试次数、随机延迟等基础参数。"""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT_DIR / "logs"
OUTPUT_DIR = ROOT_DIR / "output"
STORAGE_DIR = ROOT_DIR / "storage"
STORAGE_STATE_PATH = STORAGE_DIR / "storage_state.json"
DEFAULT_RETRY_TIMES = 3
MIN_HUMAN_DELAY_SECONDS = 0.4
MAX_HUMAN_DELAY_SECONDS = 1.2
