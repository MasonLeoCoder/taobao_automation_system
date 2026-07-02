"""主控制层：系统入口，只负责任务调度和流程编排。"""

from automation.download import run_download_workflow
from utils.logger import setup_logger


def main() -> None:
    """运行当前配置的自动化流程。

    参数：
        无。

    返回：
        无。
    """
    logger = setup_logger()
    logger.info("启动电商自动化系统")
    run_download_workflow()
    logger.info("自动化流程执行结束")


if __name__ == "__main__":
    main()
