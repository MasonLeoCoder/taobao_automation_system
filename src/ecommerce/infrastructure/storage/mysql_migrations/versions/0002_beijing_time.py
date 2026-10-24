"""将既有时间从UTC转换为北京时间，保留表关系、来源原文和幂等键。"""
import sqlalchemy as sa
from alembic import op

from ecommerce.infrastructure.storage.mysql_migrations.schema import REVISION, PREVIOUS_REVISION, build_metadata

revision = REVISION
down_revision = PREVIOUS_REVISION
branch_labels = None
depends_on = None


def time_columns():
    """列出全部受控时间字段；参数：无；返回：表及时间列。"""
    return [(table, [c for c in table.columns if isinstance(c.type, sa.DateTime)])
            for table in build_metadata().tables.values()
            if any(isinstance(c.type, sa.DateTime) for c in table.columns)]


def upgrade():
    """停写后转换一次；参数：无；返回：无，转换与版本号共同提交或回滚。"""
    connection = op.get_bind()
    tables = time_columns()
    unfinished = connection.scalar(sa.text("SELECT COUNT(*) FROM tasks WHERE finished_at IS NULL"))
    if unfinished:
        raise RuntimeError("尚有未结束任务，请先结束任务并核对最终记录，再转换数据库时间")
    for table, columns in tables:
        for column in columns:
            if connection.scalar(sa.select(sa.func.count()).select_from(table).where(column > sa.literal("9999-12-31 15:59:59.999999"))):
                raise RuntimeError("存在转换后超出DATETIME范围的时间，未转换数据")
    # MySQL DDL会隐式提交，先单独完成可重入的备注变更，再开始数据事务。
    with op.get_context().autocommit_block():
        for table, columns in tables:
            for column in columns:
                op.alter_column(table.name, column.name, existing_type=column.type,
                                existing_nullable=column.nullable, comment=column.comment)
    for table, columns in tables:
        connection.execute(table.update().values({c.name: c + sa.text("INTERVAL 8 HOUR") for c in columns}))
    # Alembic在同一迁移事务内更新版本号；失败重试不会再次给已提交的数据加8小时。


def downgrade():
    """禁止自动更改已使用的时间口径；参数：无；返回：明确说明。"""
    raise RuntimeError("北京时间转换不提供自动回退，请停写并制定包含新增数据的回退方案")
