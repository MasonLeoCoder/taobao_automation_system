"""数据库和查询统一使用北京时间；来源原文仍保留原始时区。"""
from datetime import datetime
from ecommerce.shared.time import BEIJING


def database_time(value):
    """转换带时区来源；参数：ISO字符串、时间或空值；返回：北京时间无时区值。"""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间缺少时区，不能猜测北京时间")
    return parsed.astimezone(BEIJING).replace(tzinfo=None)


def database_now():
    """生成当前数据库时间；参数：无；返回：北京时间无时区值。"""
    return datetime.now(BEIJING).replace(tzinfo=None)


def iso_value(value):
    """序列化数据库时间；参数：北京时间值或空值；返回：带+08:00的ISO字符串。"""
    if value is None:
        return None
    aware = value.replace(tzinfo=BEIJING) if value.tzinfo is None else value.astimezone(BEIJING)
    return aware.isoformat(timespec="microseconds")
