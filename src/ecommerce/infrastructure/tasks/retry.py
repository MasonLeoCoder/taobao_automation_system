"""仅对调用方明确指定的可重试异常进行重试。"""

import time
from collections.abc import Callable


def retry_call(action: Callable, exceptions: tuple[type[Exception], ...], attempts: int = 3, delay: float = 0.5):
    """执行可安全重试的调用；参数：回调、异常类型、次数、间隔；返回：回调结果。"""
    if attempts < 1 or attempts > 3:
        raise ValueError("重试次数必须为 1–3")
    for attempt in range(attempts):
        try:
            return action()
        except exceptions:
            if attempt + 1 == attempts:
                raise
            time.sleep(delay)
