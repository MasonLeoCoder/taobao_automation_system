"""看图输入的本地校验与切片；原件只读，派生 PNG 按内容寻址。"""
import hashlib
import io
import warnings
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError

VERSION = 'vision-tiles-v1'
MAX_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 80_000_000
WIDTH, HEIGHT, OVERLAP = 1280, 1600, 128


def digest(data):
    """返回字节 SHA256；参数：字节；返回：十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def original(settings, asset):
    """验证原件边界、大小与已登记摘要；参数：文件索引；返回：原始字节。"""
    root = settings.workspace.resolve(); relative = Path(asset.get('relative_path') or '')
    path = (root / relative).resolve()
    if (asset.get('root_key') != 'workspace' or asset.get('availability') != 'present'
            or relative.is_absolute() or not path.is_relative_to((root/'output').resolve()) or not path.is_file()):
        raise ValueError('本地原件缺失或目录不受支持')
    if path.stat().st_size > MAX_BYTES: raise ValueError('原件超过单张32MiB保护')
    with path.open('rb') as stream: data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or len(data) != asset.get('size_bytes') or digest(data) != asset.get('sha256'):
        raise ValueError('原件大小或SHA256与采集索引不一致')
    return data


def cached(settings, frame):
    """读取冻结切片并验证，不接受任意路径；参数：切片元信息；返回：PNG字节。"""
    value = frame.get('sha256', '')
    if len(value) != 64 or any(c not in '0123456789abcdef' for c in value): raise ValueError('图片摘要无效')
    path = settings.workspace/'output/analysis_image_inputs'/(value+'.png')
    if not path.is_file() or path.stat().st_size > MAX_BYTES: raise ValueError('分析切片已缺失或超限，请重新预览')
    data = path.read_bytes()
    if len(data) != frame['size_bytes'] or digest(data) != value: raise ValueError('分析切片校验失败，请重新预览')
    return data


def prepare(settings, asset):
    """完整顺序切片，保留原像素及重叠；参数：配置和原件；返回：尺寸与切片清单。"""
    data = original(settings, asset)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP', 'GIF'): raise ValueError('实际图片格式不受支持')
                if getattr(image, 'n_frames', 1) != 1: raise ValueError('动态图片需先明确转换为静态图片')
                if image.width * image.height > MAX_PIXELS: raise ValueError('图片超过8000万像素解码保护')
                oriented = ImageOps.exif_transpose(image)
                oriented = oriented.convert('RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB')
                width, height = oriented.size; frames = []
                directory = settings.workspace/'output/analysis_image_inputs'; directory.mkdir(parents=True, exist_ok=True)
                for top in range(0, height, HEIGHT-OVERLAP):
                    if top and top + OVERLAP >= height: break
                    for left in range(0, width, WIDTH-OVERLAP):
                        if left and left + OVERLAP >= width: break
                        box = (left, top, min(left+WIDTH, width), min(top+HEIGHT, height))
                        output = io.BytesIO(); oriented.crop(box).save(output, format='PNG')
                        content = output.getvalue()
                        if len(content) > MAX_BYTES: raise ValueError('单切片超过32MiB，未发送')
                        sha = digest(content); path = directory/(sha+'.png')
                        if path.exists():
                            if digest(path.read_bytes()) != sha: raise ValueError('切片缓存损坏，未覆盖原缓存')
                        else:
                            try:
                                with path.open('xb') as stream: stream.write(content)
                            except FileExistsError:
                                if digest(path.read_bytes()) != sha: raise ValueError('切片缓存冲突')
                        frames.append(dict(sha256=sha, size_bytes=len(content), box=list(box),
                            width=box[2]-box[0], height=box[3]-box[1], piece=len(frames)+1))
                return {'width': width, 'height': height, 'frames': frames, 'processing': VERSION}
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('图片无法安全解码') from None
