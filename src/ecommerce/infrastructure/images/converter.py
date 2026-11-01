"""真正解码和编码图片，不依赖网页、数据库或AI。"""
from io import BytesIO
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .models import ConvertedImage, ImageConversionError, MAX_FILE_BYTES, MAX_PIXELS, OUTPUT_FORMATS

FORMATS = {'PNG': ('.png', 'image/png'), 'JPEG': ('.jpg', 'image/jpeg'), 'WEBP': ('.webp', 'image/webp')}


def convert_image(data: bytes, output_format: str = 'png') -> ConvertedImage:
    """转换静态图片；参数：原始字节和目标格式；返回：编码结果，失败抛出可显示异常。"""
    if not isinstance(output_format, str) or output_format not in OUTPUT_FORMATS:
        raise ImageConversionError('请选择 PNG、JPG 或保持原格式')
    if not data or len(data) > MAX_FILE_BYTES:
        raise ImageConversionError('图片为空或超过单张 10 MB 上限')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                if getattr(probe, 'n_frames', 1) != 1:
                    raise ImageConversionError('第一版仅支持静态图片，暂不处理动图')
                source_format = probe.format
                if source_format not in FORMATS:
                    raise ImageConversionError('仅支持静态 JPG、PNG、WebP 图片')
                if probe.width * probe.height > MAX_PIXELS:
                    raise ImageConversionError('图片超过 2500 万像素上限')
                probe.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
                orientation = image.getexif().get(274, 1)
                target = {'png': 'PNG', 'jpg': 'JPEG'}.get(output_format, source_format)
                extension, mimetype = FORMATS[target]
                if output_format == 'original' or (target == source_format and orientation == 1):
                    return ConvertedImage(data, extension, mimetype, True)
                corrected = ImageOps.exif_transpose(image)
                try:
                    with BytesIO() as result:
                        if target == 'JPEG':
                            with corrected.convert('RGBA') as rgba:
                                with Image.new('RGB', rgba.size, 'white') as canvas:
                                    canvas.paste(rgba, mask=rgba.getchannel('A'))
                                    canvas.save(result, 'JPEG', quality=95, subsampling=0)
                        else:
                            # PNG只转换不支持的颜色模式；保留调色板透明度及常见原模式。
                            if corrected.mode not in {'1', 'L', 'LA', 'P', 'RGB', 'RGBA', 'I', 'I;16'}:
                                with corrected.convert('RGBA') as rgba:
                                    rgba.save(result, 'PNG')
                            else:
                                corrected.save(result, 'PNG')
                        return ConvertedImage(result.getvalue(), extension, mimetype, False)
                finally:
                    corrected.close()
    except ImageConversionError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageConversionError('图片像素过大，无法处理') from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, OverflowError):
        raise ImageConversionError('图片损坏、格式不支持或无法完整解码') from None
