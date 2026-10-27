"""仅通过项目显式升级入口运行 Alembic，连接不包含在配置文件中。"""
from alembic import context
from ecommerce.infrastructure.storage.mysql import version_table
from ecommerce.infrastructure.storage.mysql_migrations.schema_v9 import build_metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("请通过 database upgrade 命令传入迁移连接")
version_table().create(connection,checkfirst=True)
connection.commit()
context.configure(connection=connection,target_metadata=build_metadata(),transactional_ddl=False,
                  transaction_per_migration=True,compare_type=True)
with context.begin_transaction():
    context.run_migrations()
