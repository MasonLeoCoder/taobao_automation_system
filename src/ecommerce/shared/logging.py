"""项目日志配置。"""

import logging
from ecommerce.shared.config import Settings


def setup_logging(settings: Settings) -> None:
    """配置文件及终端日志；参数：Settings；返回：无。"""
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ecommerce")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    for handler in (logging.FileHandler(settings.log_dir / "run.log", encoding="utf-8"), logging.StreamHandler()):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
