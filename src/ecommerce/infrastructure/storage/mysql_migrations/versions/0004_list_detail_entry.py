"""增加列表到详情的来源外键；旧记录不推测回填，结构中断可核对重入。"""
import sqlalchemy as sa
from alembic import op

from ecommerce.infrastructure.storage.mysql_migrations.schema_v4 import REVISION, PREVIOUS_REVISION, build_metadata
from ecommerce.infrastructure.storage.mysql import _normalize_type
from ecommerce.infrastructure.storage.mysql_checks import check_expression_tree

revision = REVISION
down_revision = PREVIOUS_REVISION
branch_labels = None
depends_on = None


def upgrade():
    """添加两列与约束；参数：无；返回：无，不更改历史业务数据。"""
    connection = op.get_bind()
    if connection.scalar(sa.text('SELECT COUNT(*) FROM tasks WHERE finished_at IS NULL')):
        raise RuntimeError('尚有未结束任务，请先结束任务再升级详情来源结构')
    metadata = build_metadata()
    fields = {'search_records': 'source_list_observation_id', 'collection_runs': 'entry_record_id'}
    with op.get_context().autocommit_block():
        for name, field in fields.items():
            table = metadata.tables[name]
            column = table.c[field]
            existing = {c['name']: c for c in sa.inspect(connection).get_columns(name)}
            if field not in existing:
                op.add_column(name, sa.Column(field, column.type, nullable=True, comment=column.comment))
            else:
                actual = existing[field]
                if (_normalize_type(str(actual['type'].compile(dialect=connection.dialect))) !=
                    _normalize_type(str(column.type.compile(dialect=connection.dialect))) or
                    not actual['nullable'] or actual.get('default') is not None or actual.get('comment') != column.comment):
                    raise RuntimeError('已存在的详情来源列定义冲突：' + name + '.' + field)
            foreign = {c['name']: c for c in sa.inspect(connection).get_foreign_keys(name)}
            constraint = next(c for c in table.foreign_key_constraints if field in c.column_keys)
            if constraint.name not in foreign:
                op.create_foreign_key(constraint.name, name, constraint.referred_table.name,
                    [field], ['id'], ondelete='RESTRICT', onupdate='RESTRICT')
            else:
                actual = foreign[constraint.name]
                if (actual['constrained_columns'] != [field] or actual['referred_table'] != constraint.referred_table.name or
                    actual['referred_columns'] != ['id'] or any(actual.get('options', {}).get(k, 'RESTRICT').upper()
                    not in {'RESTRICT', 'NO ACTION'} for k in ('ondelete', 'onupdate'))):
                    raise RuntimeError('已存在的详情来源外键定义冲突：' + constraint.name)
            index = next(i for i in table.indexes if i.name in {'ix_page_list_observation', 'ix_run_entry_record'})
            indexes = {i['name']: i for i in sa.inspect(connection).get_indexes(name)}
            if index.name not in indexes:
                op.create_index(index.name, name, [c.name for c in index.columns])
            elif indexes[index.name]['column_names'] != [c.name for c in index.columns] or indexes[index.name]['unique']:
                raise RuntimeError('已存在的详情来源索引定义冲突：' + index.name)
        name = 'ck_page_list_detail'
        checks = {c['name']: c for c in sa.inspect(connection).get_check_constraints('search_records')}
        expected = next(c for c in metadata.tables['search_records'].constraints if c.name == name)
        if name not in checks:
            op.create_check_constraint(name, 'search_records', str(expected.sqltext))
        elif check_expression_tree(checks[name]['sqltext']) != check_expression_tree(str(expected.sqltext)):
            raise RuntimeError('已存在的详情来源检查约束冲突：' + name)


def downgrade():
    """避免自动丢弃来源关系；参数：无；返回：拒绝无审核回退。"""
    raise RuntimeError('详情来源结构不自动回退，请停写并审核保留来源关系的回退方案')
