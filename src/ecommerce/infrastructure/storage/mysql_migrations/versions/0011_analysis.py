"""新增五张通用分析表，不修改既有采集记录。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v11 import build_metadata, NEW_TABLES

revision = '0011_analysis'
down_revision = '0010_product_favorites'
branch_labels = None
depends_on = None


def upgrade():
    """创建五张分析表；参数：迁移连接；返回：无。"""
    metadata = build_metadata()
    for name in NEW_TABLES:
        metadata.tables[name].create(op.get_bind())


def downgrade():
    """保护已保存的分析及付费记录；参数：无；返回：拒绝隐式删表。"""
    raise RuntimeError('分析历史不可自动删除，回退须单独审核')
