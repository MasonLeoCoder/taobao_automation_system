"""日志工具：配置并获取自动化系统日志。"""

import logging

from config.settings import LOG_DIR


def setup_logger() -> logging.Logger:
    """初始化项目日志配置。

    参数：
        无。

    返回：
        项目根日志对象。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "run.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("taobao_automation_system")


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的日志对象。

    参数：
        name: 日志对象名称。

    返回：
        指定名称的日志对象。
    """
    return logging.getLogger(name)
