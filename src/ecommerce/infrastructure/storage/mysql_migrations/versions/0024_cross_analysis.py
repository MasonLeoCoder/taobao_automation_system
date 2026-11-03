"""交叉分析原生备注升级，不迁移或删除旧报告。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v24 import build_metadata, COMMENT_UPDATES

revision = '0024_cross_analysis'
down_revision = '0023_image_kind_comments'
branch_labels = depends_on = None


def upgrade():
    """同步九条COMMENT，保留全部旧数据。"""
    metadata = build_metadata()
    for table,column in COMMENT_UPDATES:
        value = metadata.tables[table].c[column]
        op.alter_column(table,column,existing_type=value.type,existing_nullable=value.nullable,comment=value.comment)


def downgrade():
    """拒绝隐式回退业务口径。"""
    raise RuntimeError('交叉分析备注不允许隐式降级')
