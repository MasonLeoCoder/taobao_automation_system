"""网页上架的素材导入和预检；不打开浏览器。"""
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import tempfile
import time
from uuid import uuid4
from zipfile import ZipFile
from xml.etree.ElementTree import ParseError

from PIL import Image
from ecommerce.infrastructure.tasks.ownership import atomic_json
from .assets import load_detail_images, load_main_images, load_portrait_images, load_sku_images
from .workbook import load_product_workbook
from .listing_profiles import ATTRIBUTES, CATEGORY, GROUPS, LINT_ROLLER

LIMIT = 512 * 1024**2
MAX_FILES = 300
NO_GUIDE_WARNING = '未提供导购标题，导购标题和 SKU 搜索标题将跳过'
IMAGE_TYPES = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP',
               '.gif': 'GIF', '.bmp': 'BMP', '.tif': 'TIFF', '.tiff': 'TIFF'}


def digest(path):
    """计算文件摘要；参数：本地副本；返回：SHA-256。"""
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _filename(name):
    """校验上传文件名；参数：浏览器文件名；返回：安全叶子名。"""
    if (not isinstance(name, str) or not name or len(name) > 180 or name != name.strip()
            or name.endswith('.') or re.search(r'[\\/:*?"<>|\x00-\x1f]', name)
            or PureWindowsPath(name).is_reserved()):
        raise ValueError('上传文件名无效，请保留普通文件名并重新选择')
    return name


def _probe_video(path):
    """使用项目现有FFmpeg解码首帧；参数：本地MP4；返回：可读时结束。"""
    from imageio_ffmpeg import get_ffmpeg_exe
    try:
        result = subprocess.run([get_ffmpeg_exe(), '-nostdin', '-v', 'error', '-xerror',
            '-protocol_whitelist', 'file', '-f', 'mov', '-i', str(path), '-map', '0:v:0',
            '-frames:v', '1', '-progress', 'pipe:1', '-f', 'null', '-'], capture_output=True, timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError('视频无法读取或解码超时') from error
    if result.returncode or not re.search(rb'^frame=[1-9]\d*\s*$', result.stdout, re.M):
        raise ValueError('视频没有可解码的画面')


def validate_titles(title, guide):
    """按发品页字符口径核对标题，导购标题可留空。"""
    if not isinstance(title, str) or not title.strip():
        raise ValueError('宝贝标题必须为非空文本')
    if not isinstance(guide, str):
        raise ValueError('导购标题必须为文本')
    title, guide = title.strip(), guide.strip()
    width = lambda value: sum(1 if ord(char) < 128 else 2 for char in value)
    if width(title) > 60:
        raise ValueError('宝贝标题超过 60 字符（汉字计 2 字符）')
    if guide and not 20 <= width(guide) <= 30:
        raise ValueError('导购标题需要 20～30 字符（汉字计 2 字符）')
    return {'title': title, 'guide': guide}


def _book(path, profile=LINT_ROLLER):
    """预检固定表格并汇总字段；参数：Excel副本；返回：预览和可跳过项。"""
    with ZipFile(path) as archive:
        if sum(info.file_size for info in archive.infolist()) > 64 * 1024**2:
            raise ValueError('Excel 解压后超过 64 MB')
    book = load_product_workbook(path)
    guide = book.fields.get('导购标题')
    titles = validate_titles(book.text('宝贝标题'), '' if guide is None else guide)
    return {**titles, 'code': book.text('商家编码'), 'price': book.fixed_price(),
            'skus': book.sku_details(), 'shop_categories': book.shop_categories(),
            'attributes': {key: str(book.fields.get(key) or '').strip() for key in profile.attributes}}


def import_materials(root, uploads, *, keyword, category, brand, profile=LINT_ROLLER, binding=None):
    """验证并封存本次选择；参数：工作目录、按角色分组的文件及导航参数；返回：预检结果。"""
    from openpyxl.utils.exceptions import InvalidFileException
    from zipfile import BadZipFile
    if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 120:
        raise ValueError('请填写有效的发品搜索词')
    if not isinstance(brand, str) or not 1 <= len(brand.strip()) <= 120:
        raise ValueError('请填写完整品牌名')
    if binding is None and (not isinstance(category, str) or [p.strip() for p in category.split('>')] != CATEGORY.split(' > ')):
        raise ValueError('首版仅支持已适配的粘毛器类目')
    if set(uploads) - profile.groups.keys() or sum(map(len, uploads.values())) > MAX_FILES:
        raise ValueError('文件分组无效，或选择超过 300 个文件')
    root.mkdir(parents=True, exist_ok=True)
    errors, warnings, skipped = [], [], []
    files, hashes, total = {}, {}, 0
    preview = {'keyword': keyword.strip(), 'category': category if binding else CATEGORY, 'brand': brand.strip()}
    identifier = uuid4().hex
    with tempfile.TemporaryDirectory(dir=root, prefix='.import-') as temporary:
        bundle = Path(temporary) / 'bundle'
        bundle.mkdir()
        for group, label in profile.groups.items():
            items = uploads.get(group, [])
            files[group] = []
            if not items:
                (errors if group in profile.required_files else warnings).append(f'{label}：未提供')
                if group not in profile.required_files:
                    skipped.append(group)
                continue
            if group in {'excel', 'video'} and len(items) != 1:
                errors.append(f'{label}：请选择唯一一个文件')
                continue
            directory = bundle / group
            directory.mkdir()
            names = set()
            for raw_name, stream in items:
                try:
                    name = _filename(raw_name)
                    suffix = Path(name).suffix.lower()
                    allowed = (('.xlsx',) if group == 'excel' else ('.mp4',) if group == 'video'
                               else IMAGE_TYPES if group == 'sku' else ('.png', '.jpg', '.jpeg'))
                    if suffix not in allowed:
                        raise ValueError('不支持此文件格式')
                    if name.casefold() in names:
                        raise ValueError('同一分组中存在重名文件')
                    names.add(name.casefold())
                    path = directory / name
                    limit = {'excel': 16, 'video': 256, 'detail': 6}.get(group, 20) * 1024**2
                    size = 0
                    with path.open('xb') as target:
                        while chunk := stream.read(1024**2):
                            size += len(chunk)
                            total += len(chunk)
                            if size > limit or total > LIMIT:
                                raise ValueError('超过单文件或整组上传大小限制')
                            target.write(chunk)
                    if not size:
                        raise ValueError('文件为空')
                    if group == 'excel':
                        preview.update(_book(path, profile))
                    elif group == 'video':
                        _probe_video(path)
                    else:
                        with Image.open(path) as image:
                            if image.format != IMAGE_TYPES[suffix]:
                                raise ValueError('图片实际格式与扩展名不符')
                            image.verify()
                        with Image.open(path) as image:
                            image.load()
                    files[group].append(name)
                    hashes[f'{group}/{name}'] = digest(path)
                except (ValueError, OSError, BadZipFile, InvalidFileException, Image.DecompressionBombError, KeyError, ParseError) as error:
                    errors.append(f'{label} / {raw_name}：{error}')
            if len(files[group]) != len(items):
                continue
            loader = {'main': load_main_images, 'portrait': load_portrait_images, 'detail': load_detail_images}.get(group)
            if loader:
                try:
                    files[group] = [path.name for path in loader(directory)]
                except ValueError as error:
                    errors.append(f'{label}：{error}')
        if 'skus' in preview:
            names = [row['销售规格'] for row in preview['skus']]
            matched = load_sku_images(bundle / 'sku', names) if files['sku'] else None
            if matched:
                for name, reason in matched.missing.items():
                    warnings.append(f'SKU 图 / {name}：{reason.replace(str(bundle / "sku"), "SKU 图分组")}，将跳过')
                unmatched = set(files['sku']) - {path.name for path in matched.files.values()}
                if unmatched:
                    warnings.append('SKU 图中未匹配的文件：' + '、'.join(sorted(unmatched)))
            preview['sku_images'] = {name: matched.files[name].name if matched and name in matched.files else None for name in names}
            for label, value in preview['attributes'].items():
                if not value:
                    warnings.append(f'Excel 未指定{label}；如果千牛使用默认项，入仓前需要人工核对')
            if not preview['guide']:
                warnings.append(NO_GUIDE_WARNING)
        result = {'id': None if errors else identifier, 'preview': preview, 'files': files,
                  'errors': errors, 'warnings': warnings, 'skipped': skipped}
        if not errors:
            manifest = {**result, 'hashes': hashes}
            if binding:
                manifest['category_binding'] = binding
            atomic_json(bundle / 'manifest.json', manifest)
            for attempt in range(3):
                try:
                    bundle.rename(root / identifier)
                    break
                except PermissionError as error:
                    if getattr(error, 'winerror', None) not in {5, 32, 33} or attempt == 2:
                        raise
                    time.sleep(.15 * (attempt + 1))
    return result
