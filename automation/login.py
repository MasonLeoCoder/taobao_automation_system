"""业务流程层：登录状态检查与登录流程管理。"""

from pathlib import Path

from browser.session import has_valid_storage_state
from config.settings import STORAGE_STATE_PATH
from utils.logger import get_logger


def ensure_login(storage_state_path: Path = STORAGE_STATE_PATH) -> bool:
    """确认是否存在可复用的登录会话。

    参数：
        storage_state_path: Playwright 登录状态文件路径。

    返回：
        如果登录状态文件存在且可用，返回 True；否则返回 False。
    """
    logger = get_logger(__name__)
    if has_valid_storage_state(storage_state_path):
        logger.info("找到可复用登录会话：%s", storage_state_path)
        return True

    logger.warning("登录状态文件不存在或格式无效：%s", storage_state_path)
    return False
