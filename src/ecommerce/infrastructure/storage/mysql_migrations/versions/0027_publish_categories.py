"""增加千牛发布类目目录、采集记录和管理菜单。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v27 import build_metadata, NEW_TABLES

revision = '0027_publish_categories'
down_revision = '0026_ledger'
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    tables = build_metadata().tables
    for name in NEW_TABLES:
        tables[name].create(connection, checkfirst=True)
    seed_menu(connection, tables['web_menu_items'])


def seed_menu(connection, menu):
    if not connection.scalar(menu.select().where(menu.c.menu_key == 'publish-categories')):
        connection.execute(menu.insert().values(menu_key='publish-categories', group_key='products',
            name='发布类目管理', sort_order=25, is_enabled=True, is_visible=True,
            construction_status='available'))


def downgrade():
    raise RuntimeError('发布类目目录不允许隐式删除')
