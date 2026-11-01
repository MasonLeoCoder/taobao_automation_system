"""商品已采集图片导出，复用转换及打包能力，原件只读。"""
import hashlib
from pathlib import Path

from ecommerce.infrastructure.images.converter import convert_image
from ecommerce.infrastructure.images.models import ImageConversionError, MAX_FILE_BYTES
from ecommerce.infrastructure.storage.main_image_export import sources
from .exports import package_images
from .service import MAX_INPUT_BYTES


def export_images(settings, product_id, batch_id, output_format, *, media_id=None, module='main_images'):
    """导出指定模块整批或一张图片；参数：配置、商品、批次、格式和媒体编号；返回：临时下载。"""
    labels={'main_images':'主图','detail_images':'详情图片'}
    if module not in labels:raise ValueError('不支持的图片导出模块')
    label_name=labels[module]
    rows = sources(settings, product_id, batch_id, media_id=media_id,module=module)
    if media_id is not None:
        rows = [row for row in rows if row['media_id'] == media_id]
        if not rows:
            raise LookupError('该图片不属于本批图片模块')
    if len(rows) > 200:
        raise ImageConversionError('单次最多下载 200 张图片，请分别下载')
    root = settings.workspace.resolve(); output_root = (root/'output').resolve()

    def converted():
        """逐张核对归属和内容哈希；参数：无；返回：原编号名称与转换结果。"""
        total = 0
        for index, asset in enumerate(rows, 1):
            label = f'{product_id}_批次{batch_id}_{label_name}{asset.get("position") or asset["media_id"]}_{asset["media_id"]}'
            try:
                relative = Path(asset.get('relative_path') or '')
                path = (root/relative).resolve()
                if asset.get('availability') != 'present' or asset.get('root_key') != 'workspace' or relative.is_absolute() or not path.is_relative_to(root) or not path.is_relative_to(output_root) or not path.is_file():
                    raise ImageConversionError('本地原件缺失或不可用')
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise ImageConversionError('图片超过单张 10 MB 上限')
                with path.open('rb') as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) != asset['size_bytes'] or hashlib.sha256(data).hexdigest() != asset['sha256']:
                    raise ImageConversionError('原件校验未通过，请核对采集日志')
                total += len(data)
                if total > MAX_INPUT_BYTES:
                    raise ImageConversionError('本批原件超过 50 MB，请分别下载')
                yield label, convert_image(data, output_format)
            except (ImageConversionError, OSError, ValueError) as error:
                reason = str(error) if isinstance(error, ImageConversionError) else '原件读取失败'
                raise ImageConversionError(f'{label}：{reason}；未输出不完整下载包。') from None
    return package_images(converted(), temp_dir=root/'output/temp/image_tools',
                          archive_name=f'{product_id}_批次{batch_id}_{label_name}.zip', force_zip=media_id is None)
