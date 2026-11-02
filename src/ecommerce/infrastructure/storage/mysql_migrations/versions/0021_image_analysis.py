"""图片分析来源扩展，既有业务行保持不变。"""
from alembic import op
from ecommerce.infrastructure.storage.mysql_migrations.schema_v21 import build_metadata, COMMENT_UPDATES, SOURCE_COMMENT

revision = '0021_image_analysis'
down_revision = '0020_composite_analysis'
branch_labels = depends_on = None


def upgrade():
    """增加可空媒体来源和约束，写入中文原生备注。"""
    metadata = build_metadata(); source = metadata.tables['analysis_sources']
    op.add_column('analysis_sources', source.c.media_id.copy())
    op.drop_constraint('ck_analysis_source_kind', 'analysis_sources', type_='check')
    check = next(c for c in source.constraints if c.name == 'ck_analysis_source_kind')
    op.create_check_constraint(check.name, 'analysis_sources', str(check.sqltext))
    op.create_foreign_key('fk_analysis_source_media', 'analysis_sources', 'media_references', ['media_id'], ['id'], ondelete='RESTRICT', onupdate='RESTRICT')
    op.create_unique_constraint('uq_analysis_source_media', 'analysis_sources', ['analysis_id', 'media_id'])
    op.create_table_comment('analysis_sources', SOURCE_COMMENT, existing_comment=None)
    for table, column in COMMENT_UPDATES:
        value = metadata.tables[table].c[column]
        op.alter_column(table, column, existing_type=value.type, existing_nullable=value.nullable, comment=value.comment)


def downgrade():
    """拒绝隐式删除图片分析来源。"""
    raise RuntimeError('图片分析契约不允许隐式降级')
