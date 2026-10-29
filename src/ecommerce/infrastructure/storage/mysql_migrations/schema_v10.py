"""第十版冻结模型：工作台共用的长期商品关注，不复制采集字段。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from ecommerce.infrastructure.storage.mysql_migrations.schema_v9 import build_metadata as previous_metadata

REVISION = '0010_product_favorites'
PREVIOUS_REVISION = '0009_collection_jobs'


def build_metadata():
    """构造关注模型；参数：无；返回：含既有表与新增关注表的元数据。"""
    metadata = previous_metadata()
    table = sa.Table('product_favorites', metadata,
        sa.Column('id', sa.String(32, collation='ascii_bin'), primary_key=True, comment='关注编号：32位小写UUID；取消后重新关注生成新编号'),
        sa.Column('product_id', mysql.BIGINT(unsigned=True), nullable=False, comment='商品内部编号：关联products.id；每件商品最多一条关注，全工作台共用'),
        sa.Column('observation_id', mysql.BIGINT(unsigned=True), nullable=False, comment='加入时列表观察编号：关联list_observations.id；业务核对同一商品且来源为已发布关键词列表'),
        sa.Column('source_keyword', sa.String(255), nullable=False, comment='加入时搜索词：从关联观察的批次读取，不从标题猜测；后续采集不覆盖'),
        sa.Column('note', sa.String(500), nullable=False, comment='人工备注：普通文字最多500字；空字符串表示未填写'),
        sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False, comment='关注时间：北京时间UTC+08:00；重复添加不覆盖，短暂撤销恢复原值'),
        sa.Column('updated_at', mysql.DATETIME(fsp=6), nullable=False, comment='最近修改时间：北京时间UTC+08:00；编辑备注时更新，采集更新不改变此字段'),
        sa.UniqueConstraint('product_id', name='uq_product_favorite_product'),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], name='fk_product_favorite_product', ondelete='RESTRICT', onupdate='RESTRICT'),
        sa.ForeignKeyConstraint(['observation_id'], ['list_observations.id'], name='fk_product_favorite_observation', ondelete='RESTRICT', onupdate='RESTRICT'),
        comment='长期商品关注表：保存商品身份、加入时依据和备注；无旧临时关注迁移，不启动采集',
        mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_bin')
    sa.Index('ix_product_favorite_created', table.c.created_at, table.c.id)
    return metadata
