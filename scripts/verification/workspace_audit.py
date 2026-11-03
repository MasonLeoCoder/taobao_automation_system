"""只读核验 workspace 迁移清单，不执行历史脚本或访问数据库。"""

import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    """计算文件摘要；参数：文件路径；返回：SHA256。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    """校验相对路径边界及链接；参数：根目录、相对路径；返回：安全路径。"""
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts:
        raise ValueError(f"路径超出允许范围：{relative}")
    target = root / part
    for item in (target, *target.parents):
        if item == root:
            break
        attributes = getattr(item.lstat(), "st_file_attributes", 0) if item.exists() else 0
        if item.is_symlink() or attributes & 0x400:
            raise ValueError(f"不允许目录链接：{relative}")
    target.resolve().relative_to(root.resolve())
    return target


def audit(root: Path, manifest: Path) -> dict:
    """只读核对迁移内容；参数：项目根目录、清单路径；返回：统计和错误。"""
    data = json.loads(manifest.read_text(encoding="utf-8"))
    errors = []
    moved = 0
    for entry in data["files"]:
        if entry["status"] != "moved":
            errors.append(f"未完成迁移：{entry['source']}")
            continue
        try:
            source = safe_path(root, entry["source"])
            target = safe_path(root, entry["target"])
            if source.exists():
                errors.append(f"旧位置重新出现文件，请人工核对：{entry['source']}")
            if not target.is_file() or target.stat().st_size != entry["size"] or digest(target) != entry["sha256"]:
                errors.append(f"目标缺失或内容不一致：{entry['target']}")
            else:
                moved += 1
        except (ValueError, OSError) as error:
            errors.append(str(error))
    loose = sorted(p.name for p in (root / "workspace").iterdir() if p.is_file())
    return {"verified_files": moved, "manifest_files": len(data["files"]), "loose_files": loose, "errors": errors}


def main() -> int:
    """解析核验命令；参数：终端参数；返回：退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--manifest", default="workspace/development/migration-manifest.json")
    args = parser.parse_args()
    root = args.root.resolve()
    result = audit(root, safe_path(root, args.manifest))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
