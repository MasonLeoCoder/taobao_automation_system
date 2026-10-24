"""第四版结构快照：关联列表观察、详情入口与商品采集运行。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from ecommerce.infrastructure.storage.mysql_migrations.schema_v3 import build_metadata as previous_metadata

REVISION = '0004_list_detail_entry'
PREVIOUS_REVISION = '0003_search_origin'


def build_metadata():
    """构造来源关联模型；参数：无；返回：带中文备注、索引及外键的模型。"""
    metadata = previous_metadata()
    pages = metadata.tables['search_records']
    pages.append_column(sa.Column('source_list_observation_id', mysql.BIGINT(unsigned=True), nullable=True,
        comment='来源列表观察编号：关联 list_observations.id；仅从列表进入详情时填写，独立入口、搜索记录或旧来源不明时为空'))
    pages.append_constraint(sa.ForeignKeyConstraint(['source_list_observation_id'], ['list_observations.id'],
        name='fk_page_list_observation', ondelete='RESTRICT', onupdate='RESTRICT', use_alter=True))
    pages.append_constraint(sa.CheckConstraint("source_list_observation_id IS NULL OR record_type = 'detail_open'",
        name='ck_page_list_detail'))
    sa.Index('ix_page_list_observation', pages.c.source_list_observation_id, pages.c.id)
    runs = metadata.tables['collection_runs']
    runs.append_column(sa.Column('entry_record_id', mysql.BIGINT(unsigned=True), nullable=True,
        comment='详情入口记录编号：关联 search_records.id 且类型为 detail_open；须同任务同商品，旧记录无可靠入口时为空'))
    runs.append_constraint(sa.ForeignKeyConstraint(['entry_record_id'], ['search_records.id'],
        name='fk_run_entry_record', ondelete='RESTRICT', onupdate='RESTRICT', use_alter=True))
    sa.Index('ix_run_entry_record', runs.c.entry_record_id, runs.c.id)
    return metadata
