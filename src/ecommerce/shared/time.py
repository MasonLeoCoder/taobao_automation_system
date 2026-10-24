"""全项目约定的北京时间，固定UTC+08:00，不依赖主机时区。"""
from datetime import timedelta, timezone

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
