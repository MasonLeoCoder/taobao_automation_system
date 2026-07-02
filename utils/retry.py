"""重试工具：为不稳定的浏览器和网络操作提供自动重试。"""

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from config.settings import DEFAULT_RETRY_TIMES

P = ParamSpec("P")
R = TypeVar("R")


def retry(max_attempts: int = DEFAULT_RETRY_TIMES) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """在函数抛出异常时自动重试。

    参数：
        max_attempts: 最大尝试次数，超过后重新抛出最后一次异常。

    返回：
        带自动重试能力的装饰器。
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        """为目标函数添加重试行为。

        参数：
            func: 需要包装的目标函数。

        返回：
            已添加重试行为的函数。
        """

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            """执行带重试行为的目标函数。

            参数：
                *args: 传给目标函数的位置参数。
                **kwargs: 传给目标函数的关键字参数。

            返回：
                目标函数的执行结果。
            """
            last_error: Exception | None = None
            for _ in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as error:
                    last_error = error

            if last_error is not None:
                raise last_error

            raise RuntimeError("重试失败，但未捕获到具体异常")

        return wrapper

    return decorator
