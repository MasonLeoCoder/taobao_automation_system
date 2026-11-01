"""图片转换业务：受限输入、逐文件错误和共享下载产物。"""
from ecommerce.infrastructure.images.converter import convert_image
from ecommerce.infrastructure.images.models import ImageConversionError, MAX_FILE_BYTES
from .exports import package_images, safe_name

MAX_UPLOAD_FILES = 20
MAX_INPUT_BYTES = 50 * 1024 * 1024


def convert_uploads(files, output_format, *, temp_dir):
    """处理上传文件流；参数：名称/流列表、目标格式及临时目录；返回：单图或ZIP下载。"""
    if not 1 <= len(files) <= MAX_UPLOAD_FILES:
        raise ImageConversionError('请一次选择 1 至 20 张图片')

    def converted():
        """逐张限量读取并转换；参数：无；返回：名称和编码结果。"""
        total = 0
        for name, source in files:
            label = safe_name(name)
            try:
                data = source.read(MAX_FILE_BYTES + 1)
                total += len(data)
                if total > MAX_INPUT_BYTES:
                    raise ImageConversionError('本次图片总大小超过 50 MB')
                yield label, convert_image(data, output_format)
            except ImageConversionError as error:
                raise ImageConversionError(f'“{label}”：{error}；本次未输出下载包，可移除该文件后重试。') from None
    return package_images(converted(), temp_dir=temp_dir)
