"""下载输出命名和临时压缩包，不写入商品资料。"""
from dataclasses import dataclass
from pathlib import Path
import re
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from zipfile import ZipFile, ZIP_STORED

from ecommerce.infrastructure.images.models import ImageConversionError

MAX_OUTPUT_BYTES = 150 * 1024 * 1024


@dataclass
class DownloadArtifact:
    """请求内下载结果；调用方负责关闭stream，关闭时释放临时内容。"""
    stream: BinaryIO
    filename: str
    mimetype: str
    count: int


def safe_name(name: str) -> str:
    """生成可跨平台下载的文件名；参数：建议名称；返回：无路径和控制字符的名称。"""
    name = re.split(r'[/\\]', str(name))[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '_', name).strip(' .')[:120]
    if not name or name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        name = '图片'
    return name


def package_images(images, *, temp_dir: Path, archive_name='图片转换.zip', force_zip=False) -> DownloadArtifact:
    """逐张写入临时输出；参数：名称/结果迭代、临时目录和包名；返回：全成功下载，失败全部关闭。"""
    temp_dir.mkdir(parents=True, exist_ok=True)
    stream = SpooledTemporaryFile(max_size=8*1024*1024, mode='w+b', dir=temp_dir)
    try:
        iterator = iter(images)
        first = next(iterator, None)
        if first is None:
            raise ImageConversionError('没有可下载的图片')
        second = next(iterator, None)
        used = set(); total = 0; count = 0

        def named(item):
            """检查输出大小并去重；参数：文件名和编码结果；返回：安全名称和字节。"""
            nonlocal total, count
            label, image = item
            total += len(image.data); count += 1
            if total > MAX_OUTPUT_BYTES:
                raise ImageConversionError('转换结果超过 150 MB，请减少图片数量')
            stem = Path(safe_name(label)).stem or '图片'
            name = stem + image.extension; number = 2
            while name.casefold() in used:
                name = f'{stem}_{number}{image.extension}'; number += 1
            used.add(name.casefold())
            return name, image.data

        if second is None and not force_zip:
            name, data = named(first); stream.write(data); mimetype = first[1].mimetype
        else:
            from itertools import chain
            with ZipFile(stream, 'w', compression=ZIP_STORED) as archive:
                for item in chain((first,), (second,) if second else (), iterator):
                    name, data = named(item); archive.writestr(name, data)
            name = safe_name(archive_name); mimetype = 'application/zip'
        stream.seek(0)
        return DownloadArtifact(stream, name, mimetype, count)
    except BaseException:
        stream.close()
        raise
