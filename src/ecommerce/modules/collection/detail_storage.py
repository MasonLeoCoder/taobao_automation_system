"""详情资料按商品 ID 汇总；各采集步骤合并自己的分区，历史批次独立保留。"""
import json
import re
from pathlib import Path
from uuid import uuid4

from ecommerce.infrastructure.storage.database import now


def product_directory(settings, item_id):
    if not isinstance(item_id, str) or not re.fullmatch(r"[0-9]{1,25}", item_id) or int(item_id) == 0:
        raise ValueError("商品资料目录需要有效的数字商品 ID")
    return settings.workspace / "output/raw/taobao_details" / item_id


def read_detail_document(directory, item_id):
    destination = Path(directory) / f"{item_id}.json"
    document = {}
    if destination.exists():
        document = json.loads(destination.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("product_id") != item_id:
            raise ValueError("现有详情 JSON 的商品 ID 不匹配，未覆盖")
    from ecommerce.modules.collection.persistence import read_database_document
    database_document=read_database_document(directory,item_id)
    if database_document is not None:
        document={**{k:v for k,v in document.items() if k in {'schema_version','product_id','created_at','updated_at'}},**database_document}
    return document


def save_detail_section(directory, item_id, section, value):
    """原子更新一个分区，保留其他已采集的详情资料。"""
    return save_detail_sections(directory, item_id, {section: value})


def save_detail_sections(directory, item_id, sections):
    """同一批商品信息和 SKU 明细一次发布，避免只更新一半。"""
    destination = Path(directory) / f"{item_id}.json"
    document = read_detail_document(directory, item_id)
    document.setdefault("created_at", now())
    document.update(schema_version=2, product_id=item_id, updated_at=now())
    document.update(sections)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{item_id}-{uuid4().hex}.json.tmp")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    from ecommerce.modules.collection.persistence import capture_sections
    capture_sections(directory,item_id,sections)
    return destination
