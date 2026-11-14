"""新增平台账号、店铺关联及账号管理入口。"""

from alembic import op

from ecommerce.infrastructure.storage.mysql_migrations.schema_v29 import NEW_TABLES, build_metadata

revision = '0029_platform_accounts'
down_revision = '0028_sycm_search'
branch_labels = depends_on = None


def seed_menu(connection, menu):
    if not connection.scalar(menu.select().where(menu.c.menu_key == 'platform-accounts')):
        connection.execute(menu.insert().values(menu_key='platform-accounts', group_key='system',
            name='账号管理', sort_order=35, is_enabled=True, is_visible=True, construction_status='available'))


def upgrade():
    connection = op.get_bind()
    tables = build_metadata().tables
    for name in NEW_TABLES:
        tables[name].create(connection, checkfirst=True)
    seed_menu(connection, tables['web_menu_items'])


def downgrade():
    raise RuntimeError('账号和店铺登记不允许隐式删除')
