"""第五版结构快照：保留实时采集，移除历史导入管理。"""
from ecommerce.infrastructure.storage.mysql_migrations.schema_v4 import build_metadata as previous_metadata

REVISION = '0005_remove_history_import'
PREVIOUS_REVISION = '0004_list_detail_entry'
IMPORT_TABLES = ('import_issues', 'import_row_mappings', 'import_sources', 'import_runs')
OWNER_TABLES = ('tasks', 'task_events', 'search_records', 'collection_runs', 'collection_batches')


def build_metadata():
    """构造精简后的独立模型；参数：无；返回：含完整备注与业务约束的元数据。"""
    metadata = previous_metadata()
    for name in OWNER_TABLES:
        table = metadata.tables[name]
        column = table.c.import_run_id
        for constraint in list(table.foreign_key_constraints):
            if 'import_run_id' in constraint.column_keys:
                table.constraints.remove(constraint)
                for foreign in constraint.elements:
                    table.foreign_keys.remove(foreign)
                    column.foreign_keys.remove(foreign)
        # 仅修改本次新建的模型实例；旧版构造器和已执行迁移保持冻结。
        table._columns.remove(column)
    for name in IMPORT_TABLES:
        metadata.remove(metadata.tables[name])
    return metadata
