"""新增长期商品关注表；不改写原有业务记录和历史迁移。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v10 import build_metadata

revision = '0010_product_favorites'
down_revision = '0009_collection_jobs'
branch_labels = None
depends_on = None


def upgrade():
    """创建关注表及约束；参数：迁移连接；返回：无。"""
    build_metadata().tables['product_favorites'].create(op.get_bind())


def downgrade():
    """拒绝隐式删除长期关注；参数：无；返回：异常。"""
    raise RuntimeError('长期关注不可自动删除，回退须单独审核')
