"""增加结构化销量，旧记录待解析；不改原文，支持中断后结构重入。"""
from alembic import op
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v6 import build_metadata

revision = '0006_list_sales'
down_revision = '0005_remove_history_import'
branch_labels = None
depends_on = None


def upgrade():
    """预检任务后增加字段及约束；参数：无；返回：无，历史数值另行回填。"""
    connection = op.get_bind()
    if connection.scalar(sa.text('SELECT COUNT(*) FROM tasks WHERE finished_at IS NULL')):
        raise RuntimeError('存在未结束任务，暂不升级销量结构')
    expected = build_metadata().tables['list_observations']
    names = ('sales_count', 'sales_precision', 'sales_metric', 'sales_period', 'sales_parse_status')
    inspector = sa.inspect(connection)
    actual = {column['name']: column for column in inspector.get_columns(expected.name)}
    for name in names:
        if name in actual:
            column = expected.c[name]
            if (str(actual[name]['type']).lower() != str(column.type).lower() or actual[name]['nullable'] != column.nullable
                    or actual[name].get('comment') != column.comment):
                raise RuntimeError('已存在的销量字段与迁移定义不一致：' + name)
    with op.get_context().autocommit_block():
        for name in names:
            if name not in actual:
                op.add_column(expected.name, expected.c[name]._copy())
        existing = {c['name'] for c in sa.inspect(connection).get_check_constraints(expected.name)}
        for constraint in sorted(expected.constraints, key=lambda c: c.name or ''):
            if isinstance(constraint, sa.CheckConstraint) and constraint.name.startswith('ck_list_sales_') and constraint.name not in existing:
                op.create_check_constraint(constraint.name, expected.name, str(constraint.sqltext))
        indexes = {i['name'] for i in sa.inspect(connection).get_indexes(expected.name)}
        if 'ix_list_sales_count' not in indexes:
            op.create_index('ix_list_sales_count', expected.name, ['sales_count', 'id'])


def downgrade():
    """拒绝自动丢弃销量结构；参数：无；返回：明确异常。"""
    raise RuntimeError('销量字段已保存业务结果，如需回退请单独审核')
