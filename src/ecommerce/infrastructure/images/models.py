"""图片转换的输入限制、输出及可显示异常。"""
from dataclasses import dataclass


class ImageConversionError(ValueError):
    """可以向用户展示的单文件处理问题。"""


@dataclass(frozen=True)
class ConvertedImage:
    """转换结果；data为实际编码字节，extension和mimetype以内容为准。"""
    data: bytes
    extension: str
    mimetype: str
    reused: bool


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000
OUTPUT_FORMATS = {'png', 'jpg', 'original'}
