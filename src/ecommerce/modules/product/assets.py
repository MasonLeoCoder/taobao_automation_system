"""商品本地图片输入检查。"""
from pathlib import Path
from dataclasses import dataclass
from logging import getLogger


def load_main_images(directory: Path) -> list[Path]:
    """读取五张主图；参数：图片目录；返回：按 1～5 排序的绝对路径。"""
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError(f"主图目录不存在：{directory}")
    files = [path for path in directory.iterdir() if path.is_file()
             and path.suffix.lower() in {".png", ".jpg", ".jpeg"}]
    expected = {"1", "2", "3", "4", "5"}
    stems = {path.stem for path in files}
    if len(files) != 5 or stems != expected:
        missing = "、".join(sorted(expected - stems))
        message = f"需要 5 张，已找到 {len(files)} 张。文件名须为 1～5，支持 PNG、JPG 和 JPEG，不能重号"
        raise ValueError(f"{message}。缺少编号 {missing}" if missing else message)
    for path in files:
        if not 0 < path.stat().st_size <= 20 * 1024 * 1024:
            raise ValueError(f"主图为空或超过 20MB：{path.name}")
    return sorted(files, key=lambda path: int(path.stem))


def load_portrait_images(directory: Path) -> list[Path]:
    """读取3:4主图；参数：目录；返回：编号1～5，或1～4加唯一未编号图片。"""
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError(f"3:4 主图目录不存在：{directory}")
    files = [path for path in directory.iterdir() if path.is_file()
             and path.suffix.lower() in {".png", ".jpg", ".jpeg"}]
    numbered = [path for path in files if path.stem in {"1", "2", "3", "4", "5"}]
    other = [path for path in files if path not in numbered]
    stems = {path.stem for path in numbered}
    if len(files) != 5 or not (
        (len(numbered) == 5 and stems == {"1", "2", "3", "4", "5"})
        or (len(numbered) == 4 and stems == {"1", "2", "3", "4"}
            and len(other) == 1 and not other[0].stem.isdecimal())
    ):
        raise ValueError(f"需要 5 张，已找到 {len(files)} 张。请使用编号 1～5，或编号 1～4 加一张未编号图片")
    for path in files:
        if not 0 < path.stat().st_size <= 20 * 1024 * 1024:
            raise ValueError(f"3:4 主图为空或超过20MB：{path.name}")
    return sorted(numbered, key=lambda path: int(path.stem)) + other


def load_detail_images(directory: Path) -> list[Path]:
    """读取全部详情图；参数：目录；返回：按正整数文件名排序的图片路径。"""
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError(f"详情图目录不存在：{directory}")
    files = [path for path in directory.iterdir() if path.is_file()
             and path.suffix.lower() in {".png", ".jpg", ".jpeg"}]
    if not files:
        raise ValueError("详情图目录中没有 png/jpg/jpeg 图片")
    if any(not path.stem.isascii() or not path.stem.isdecimal() or int(path.stem) < 1 for path in files):
        raise ValueError("详情图文件名必须为正整数编号，例如1.jpg、2.jpg、10.jpg")
    numbers = [int(path.stem) for path in files]
    if len(numbers) != len(set(numbers)):
        raise ValueError("详情图编号不能重复，例如1.jpg与01.png属于同一编号")
    for path in files:
        if not 0 < path.stat().st_size <= 6 * 1024 * 1024:
            raise ValueError(f"详情图为空或超过6MB：{path.name}")
    return sorted(files, key=lambda path: int(path.stem))


def load_product_video(path: Path) -> Path:
    """检查本地商品视频；参数：MP4路径；返回：存在且非空的绝对路径。"""
    path = path.resolve()
    if not path.is_file() or path.suffix.lower() != ".mp4":
        raise ValueError(f"商品视频必须是存在的MP4文件：{path}")
    if not path.stat().st_size:
        raise ValueError(f"商品视频为空：{path.name}")
    return path


@dataclass(frozen=True)
class SkuImageInputs:
    """规格名对应的唯一图片及跳过原因；不改变本地素材。"""
    files: dict[str, Path]
    missing: dict[str, str]


def load_sku_images(directory: Path, names: list[str], *, codes=None, requested=None) -> SkuImageInputs:
    """按指定图名或商家编码、规格名匹配，返回每条 SKU 的文件和跳过原因。"""
    directory = directory.resolve()
    if not names or any(not isinstance(name, str) or not name.strip() for name in names) or len(set(names)) != len(names):
        raise ValueError("SKU图片匹配需要非空、唯一的销售规格名称")
    files, missing = {}, {}
    directory_error = None
    try:
        candidates = [path for path in directory.iterdir() if path.is_file() and path.suffix] if directory.is_dir() else []
    except OSError as error:
        candidates = []
        directory_error = f"SKU图片目录无法读取：{directory}；原因={error}"
    requested = {name: stem.strip() for name, stem in (requested or {}).items() if stem.strip()}
    for name in names:
        stem = requested.get(name)
        code = (codes or {}).get(name)
        if stem:
            matches = [path for path in candidates if path.stem == stem]
        else:
            matches = [path for path in candidates if path.stem.casefold() == code.casefold()] if code else []
            if not matches:
                matches = [path for path in candidates if path.stem == name]
        reason = None
        if directory_error:
            reason = directory_error
        elif stem and not matches:
            reason = f"未找到指定图片「{stem}」，请核对 SKU 图文件夹"
        elif not directory.is_dir():
            reason = f"SKU图片目录不存在：{directory}"
        elif not matches:
            reason = f"未找到图片：{directory / (name + '.*')}"
        elif len(matches) != 1:
            reason = "同名图片不唯一：" + "、".join(sorted(path.name for path in matches))
        elif not stem and code and matches[0].stem.casefold() == code.casefold() and sum(
            other.casefold() == code.casefold() for other in codes.values()
        ) > 1:
            reason = f"商家编码「{code}」对应多条 SKU：{matches[0].name}"
        else:
            try:
                if not 0 < matches[0].stat().st_size <= 20 * 1024 * 1024:
                    reason = f"SKU图片为空或超过20MB：{matches[0].name}"
            except OSError as error:
                reason = f"SKU图片无法读取：{error}"
        if reason:
            missing[name] = reason
            getLogger(__name__).warning("SKU图片跳过：规格=%s；缺少SKU搜索主图、商品规格图片；原因=%s", name, reason)
        else:
            files[name] = matches[0]
    if codes is not None or requested:
        for file in set(files.values()):
            owners = [name for name, path in files.items() if path == file]
            if len(owners) > 1:
                for name in (name for name in owners if name not in requested):
                    del files[name]
                    missing[name] = f"图片「{file.name}」同时匹配多条 SKU：" + "、".join(owners)
                    getLogger(__name__).warning("SKU图片跳过：规格=%s；缺少SKU搜索主图、商品规格图片；原因=%s",
                                                name, missing[name])
    return SkuImageInputs(files, missing)
