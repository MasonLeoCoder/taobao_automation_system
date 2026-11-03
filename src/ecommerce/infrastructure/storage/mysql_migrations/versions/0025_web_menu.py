"""新增菜单配置与冻结初始行；不覆盖用户配置，不更改旧业务数据。"""
from alembic import op
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v25 import build_metadata, NEW_TABLES
from ecommerce.infrastructure.storage.mysql_migrations.menu_seed_v25 import GROUP_ROWS, ITEM_ROWS

revision = '0025_web_menu'
down_revision = '0024_cross_analysis'
branch_labels = depends_on = None


def seed_missing(connection):
    """仅补充缺失初始项；参数：迁移连接；返回：无，不覆盖已存在的自定义行。"""
    tables = build_metadata().tables
    for name, key, rows in (('web_menu_groups', 'group_key', GROUP_ROWS), ('web_menu_items', 'menu_key', ITEM_ROWS)):
        table = tables[name]
        known = set(connection.scalars(sa.select(table.c[key])))
        for row in rows:
            if row[key] not in known:
                connection.execute(table.insert().values(**row))


def upgrade():
    """创建配置表并初始化6组31项；参数：无；返回：无，页面访问不调用此函数。"""
    connection = op.get_bind()
    tables = build_metadata().tables
    for name in NEW_TABLES:
        tables[name].create(connection)
    seed_missing(connection)


def downgrade():
    """拒绝隐式丢弃用户菜单配置；参数：无；返回：异常。"""
    raise RuntimeError('菜单配置不允许隐式删除，回退须单独审核')
