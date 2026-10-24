"""当前结构：首版约束保持不变，时间字段统一北京时间。"""
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v1 import build_metadata as previous_metadata

REVISION = "0002_beijing_time"
PREVIOUS_REVISION = "0001_mysql_foundation"


def build_metadata():
    """构造当前字段契约；参数：无；返回：含北京时间备注的完整模型。"""
    metadata = previous_metadata()
    for table in metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, sa.DateTime):
                column.comment = column.comment.replace("UTC 时间", "北京时间（UTC+08:00）")
    return metadata
