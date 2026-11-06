"""新增生意参谋搜索排行快照和网页入口。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v28 import build_metadata, NEW_TABLES

revision = '0028_sycm_search'
down_revision = '0027_publish_categories'
branch_labels = depends_on = None


def seed_menu(connection, menu):
    if not connection.scalar(menu.select().where(menu.c.menu_key == 'sycm-search')):
        connection.execute(menu.insert().values(menu_key='sycm-search', group_key='research',
            name='生意参谋搜索排行', sort_order=45, is_enabled=True, is_visible=True,
            construction_status='available'))


def upgrade():
    connection = op.get_bind()
    tables = build_metadata().tables
    for name in NEW_TABLES:
        tables[name].create(connection, checkfirst=True)
    seed_menu(connection, tables['web_menu_items'])


def downgrade():
    raise RuntimeError('排行采集快照不允许隐式删除')
