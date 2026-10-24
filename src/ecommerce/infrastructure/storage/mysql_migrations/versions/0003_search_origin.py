"""新增独立搜索来源列并按原记录回填；结构中断可核对后重入。"""
import sqlalchemy as sa
from alembic import op

from ecommerce.infrastructure.storage.mysql_migrations.schema import build_metadata as old_metadata
from ecommerce.infrastructure.storage.mysql_migrations.schema_v3 import REVISION, PREVIOUS_REVISION, build_metadata
from ecommerce.infrastructure.storage.mysql_migrations.search_origin_v3 import ORIGIN_COLUMNS, enrich_origin
from ecommerce.infrastructure.storage.mysql_checks import check_expression_tree

revision = REVISION
down_revision = PREVIOUS_REVISION
branch_labels = None
depends_on = None


def upgrade():
    """检查来源后添加结构并事务回填；参数：无；返回：无，失败保留旧版本及可识别结构。"""
    connection = op.get_bind()
    from ecommerce.infrastructure.storage.mysql import _normalize_type
    if connection.scalar(sa.text('SELECT COUNT(*) FROM tasks WHERE finished_at IS NULL')):
        raise RuntimeError('尚有未结束任务，请先结束任务再升级搜索来源结构')
    previous, expected = old_metadata(), build_metadata()
    plans = []
    for name in ORIGIN_COLUMNS:
        # 先验证全部原记录，遇到冲突时不执行DDL，也不更改历史payload。
        inspector = sa.inspect(connection)
        present = {c['name'] for c in inspector.get_columns(name)}
        selected = [c for c in expected.tables[name].c if c.name in present]
        for row in connection.execute(sa.select(*selected)).mappings():
            values = {column.name: row[column.name] for column in previous.tables[name].c}
            enriched = enrich_origin(name, values, row)
            plans.append((name, row['id'], {key: enriched[key] for key in ORIGIN_COLUMNS[name]}))
    # MySQL结构操作会隐式提交；逐对象核对，重入时仅补齐缺少的对象。
    with op.get_context().autocommit_block():
        for name, fields in ORIGIN_COLUMNS.items():
            table = expected.tables[name]
            present = {c['name']: c for c in sa.inspect(connection).get_columns(name)}
            for key in fields:
                column = table.c[key]
                if key not in present:
                    op.add_column(name, sa.Column(key, column.type, nullable=True, comment=column.comment))
                elif _normalize_type(str(present[key]['type'].compile(dialect=connection.dialect))) != _normalize_type(str(column.type.compile(dialect=connection.dialect))):
                    raise RuntimeError('已存在的来源列类型冲突：' + name + '.' + key)
                elif not present[key]['nullable'] or present[key].get('comment') != column.comment:
                    raise RuntimeError('已存在的来源列定义冲突：' + name + '.' + key)
                elif getattr(column.type,'collation',None) and getattr(present[key]['type'],'collation',None) != column.type.collation:
                    raise RuntimeError('已存在的来源列排序规则冲突：' + name + '.' + key)
            old_table = previous.tables[name]
            constraints = {c['name']: c for c in sa.inspect(connection).get_foreign_keys(name)}
            for constraint in sorted(table.foreign_key_constraints, key=lambda c: c.name):
                if constraint.name not in {c.name for c in old_table.foreign_key_constraints} and constraint.name not in constraints:
                    op.create_foreign_key(constraint.name, name, constraint.referred_table.name,
                        [e.parent.name for e in constraint.elements], [e.column.name for e in constraint.elements],
                        ondelete='RESTRICT', onupdate='RESTRICT')
                elif constraint.name not in {c.name for c in old_table.foreign_key_constraints}:
                    actual = constraints[constraint.name]
                    if (actual['constrained_columns'] != [e.parent.name for e in constraint.elements]
                            or actual['referred_table'] != constraint.referred_table.name
                            or actual['referred_columns'] != [e.column.name for e in constraint.elements]
                            or any(actual.get('options', {}).get(k, 'RESTRICT') != 'RESTRICT' for k in ('ondelete','onupdate'))):
                        raise RuntimeError('已存在的来源外键定义冲突：' + constraint.name)
            checks = {c['name']: c for c in sa.inspect(connection).get_check_constraints(name)}
            for constraint in sorted(table.constraints, key=lambda c: c.name or ''):
                if isinstance(constraint, sa.CheckConstraint) and constraint.name not in checks:
                    op.create_check_constraint(constraint.name, name, constraint.sqltext)
                elif isinstance(constraint, sa.CheckConstraint) and check_expression_tree(str(constraint.sqltext)) != check_expression_tree(checks[constraint.name]['sqltext']):
                    raise RuntimeError('已存在的来源检查约束冲突：' + constraint.name)
            indexes = {i['name']: i for i in sa.inspect(connection).get_indexes(name)}
            for index in sorted(table.indexes, key=lambda i: i.name):
                if index.name not in indexes:
                    op.create_index(index.name, name, [c.name for c in index.columns], unique=index.unique)
                elif indexes[index.name]['column_names'] != [c.name for c in index.columns] or bool(indexes[index.name]['unique']) != bool(index.unique):
                    raise RuntimeError('已存在的来源索引定义冲突：' + index.name)
    for name, identifier, values in plans:
        table = expected.tables[name]
        connection.execute(table.update().where(table.c.id == identifier).values(**values))
    # 数据更新及Alembic版本推进同一事务提交；中断后结构保留，数据回滚，重试不重复转换。


def downgrade():
    """保留已使用的来源信息；参数：无；返回：明确拒绝自动丢弃列。"""
    raise RuntimeError('搜索来源结构不自动回退，请停写并审核包含新增来源数据的回退方案')
