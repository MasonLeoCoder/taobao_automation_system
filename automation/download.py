"""业务流程层：下载流程编排。"""

from automation.login import ensure_login
from utils.logger import get_logger


def run_download_workflow() -> None:
    """运行标准下载流程。

    参数：
        无。

    返回：
        无。
    """
    logger = get_logger(__name__)
    logger.info("准备执行下载流程")
    session_ready = ensure_login()

    if not session_ready:
        logger.warning("登录会话不可用，下载流程已停止")
        return

    logger.info("下载流程骨架已就绪，等待补充浏览器执行逻辑")
