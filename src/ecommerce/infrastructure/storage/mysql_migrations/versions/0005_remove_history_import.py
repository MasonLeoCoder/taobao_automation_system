"""仅在无导入记录、无活动任务时移除历史导入结构；支持DDL中断重入。"""
from alembic import op
import sqlalchemy as sa

from ecommerce.infrastructure.storage.mysql_migrations.schema_v5 import IMPORT_TABLES, OWNER_TABLES

revision = '0005_remove_history_import'
down_revision = '0004_list_detail_entry'
branch_labels = None
depends_on = None


def upgrade():
    """预检通过后删除空导入结构；参数：无；返回：无，不改写业务记录。"""
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    names = set(inspector.get_table_names())
    if connection.scalar(sa.text('SELECT COUNT(*) FROM tasks WHERE finished_at IS NULL')):
        raise RuntimeError('尚有未结束任务，请先正常结束任务再移除历史导入结构')
    for name in IMPORT_TABLES:
        if name in names and connection.scalar(sa.text('SELECT COUNT(*) FROM `' + name + '`')):
            raise RuntimeError('导入表仍有记录，未删除：' + name)
    for name in OWNER_TABLES:
        if 'import_run_id' in {column['name'] for column in inspector.get_columns(name)}:
            if connection.scalar(sa.text('SELECT COUNT(*) FROM `' + name + '` WHERE import_run_id IS NOT NULL')):
                raise RuntimeError('业务记录仍关联历史导入，未删除：' + name + '.import_run_id')
    # 所有检查在首个DDL之前完成；陌生引用不能随着精简被自动解除。
    for name in names:
        for foreign in inspector.get_foreign_keys(name):
            if foreign['referred_table'] in IMPORT_TABLES and name not in IMPORT_TABLES:
                if (name not in OWNER_TABLES or foreign['name'] != 'fk_' + name + '_import' or
                    foreign['constrained_columns'] != ['import_run_id'] or foreign['referred_table'] != 'import_runs' or
                    foreign['referred_columns'] != ['id']):
                    raise RuntimeError('发现未确认的导入外键依赖，未删除：' + name + '.' + foreign['name'])
    with op.get_context().autocommit_block():
        for name in OWNER_TABLES:
            foreign = {item['name']: item for item in sa.inspect(connection).get_foreign_keys(name)}
            constraint = 'fk_' + name + '_import'
            if constraint in foreign:
                op.drop_constraint(constraint, name, type_='foreignkey')
            if 'import_run_id' in {column['name'] for column in sa.inspect(connection).get_columns(name)}:
                op.drop_column(name, 'import_run_id')
        for name in IMPORT_TABLES:
            if sa.inspect(connection).has_table(name):
                op.drop_table(name)


def downgrade():
    """精简不自动恢复已退役功能；参数：无；返回：拒绝未审核回退。"""
    raise RuntimeError('历史导入能力已移除；如需恢复，请另行审核代码与结构方案')
