"""操作系统级文件锁，进程退出自动释放。"""

import os
from contextlib import contextmanager
from pathlib import Path
from ecommerce.shared.exceptions import TaskBusy


@contextmanager
def shop_lock(profile_path: Path):
    """锁定共享浏览器资料；参数：资料目录；返回：持锁上下文。"""
    profile_path.mkdir(parents=True, exist_ok=True)
    path = profile_path / ".ecommerce.lock"
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise TaskBusy("店铺浏览器已有任务运行，请等待其结束") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
