"""新增手动账本并标记菜单已接入，不覆盖用户菜单配置。"""
from alembic import op
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v26 import build_metadata, NEW_TABLES

revision = '0026_ledger'
down_revision = '0025_web_menu'
branch_labels = depends_on = None


def upgrade():
    """创建三表与初始分类；参数：无；返回：无，不生成示例账目或预算。"""
    connection = op.get_bind()
    tables = build_metadata().tables
    for name in NEW_TABLES:
        tables[name].create(connection)
    for number, name in enumerate(('代发采购', '补单', '其他支出', '提现', '其他收入'), 1):
        connection.execute(tables['ledger_categories'].insert().values(name=name, sort_order=number*10,
            is_enabled=True, created_at=sa.func.current_timestamp(6), updated_at=sa.func.current_timestamp(6)))
    menu = tables['web_menu_items']
    connection.execute(menu.update().where(menu.c.menu_key == 'ledger').values(construction_status='available'))


def downgrade():
    """保护账本不隐式删除；参数：无；返回：异常。"""
    raise RuntimeError('经营账本不允许隐式删除')
