"""创建带中文备注、外键和检查约束的首版 MySQL 结构。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v1 import REVISION,build_metadata

revision=REVISION
down_revision=None
branch_labels=None
depends_on=None


def upgrade():
    """按冻结版本建表；参数：无；返回：无，失败保留结构供核对。"""
    build_metadata().create_all(op.get_bind(),checkfirst=False)


def downgrade():
    """拒绝自动删库式回退；参数：无；返回：抛出说明。"""
    raise RuntimeError("首版结构不提供自动删表回退，请先备份并制定保留新增数据的回退方案")
