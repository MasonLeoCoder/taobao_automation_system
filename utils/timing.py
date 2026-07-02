"""时间工具：提供接近人工操作节奏的随机延迟。"""

import random
import time

from config.settings import MAX_HUMAN_DELAY_SECONDS, MIN_HUMAN_DELAY_SECONDS


def human_delay(
    min_seconds: float = MIN_HUMAN_DELAY_SECONDS,
    max_seconds: float = MAX_HUMAN_DELAY_SECONDS,
) -> None:
    """暂停一段随机时间，模拟人工操作间隔。

    参数：
        min_seconds: 最短延迟秒数。
        max_seconds: 最长延迟秒数。

    返回：
        无。
    """
    time.sleep(random.uniform(min_seconds, max_seconds))
