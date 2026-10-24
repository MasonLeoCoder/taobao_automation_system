"""第三版结构快照：增加独立搜索来源，保留已执行版本模型。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from ecommerce.infrastructure.storage.mysql_migrations.schema import build_metadata as previous_metadata

REVISION = '0003_search_origin'
PREVIOUS_REVISION = '0002_beijing_time'


def build_metadata():
    """构造第三版模型；参数：无；返回：含中文备注、来源约束及索引的完整模型。"""
    metadata = previous_metadata()
    for name in ('collection_batches', 'search_records'):
        table = metadata.tables[name]
        table.append_column(sa.Column('search_method', sa.String(24, collation='ascii_bin'), nullable=True,
            comment='搜索方式：keyword 关键词、image 以图搜图、unknown 已确认搜索但方式未知；NULL 表示未记录或不适用'))
        table.append_column(sa.Column('search_image_file_id', mysql.BIGINT(unsigned=True), nullable=True,
            comment='搜索输入图片文件编号：关联 file_assets.id；关键词搜索或来源未提供时为空，不是结果商品主图'))
        prefix = 'batch' if name == 'collection_batches' else 'page'
        table.append_constraint(sa.ForeignKeyConstraint(['search_image_file_id'], ['file_assets.id'],
            name='fk_' + prefix + '_search_image', ondelete='RESTRICT', onupdate='RESTRICT', use_alter=True))
        table.append_constraint(sa.CheckConstraint("search_method IS NULL OR search_method IN ('keyword','image','unknown')",
            name='ck_' + prefix + '_search_method'))
        table.append_constraint(sa.CheckConstraint("search_image_file_id IS NULL OR (search_method IS NOT NULL AND search_method = 'image')",
            name='ck_' + prefix + '_search_image'))
        sa.Index('ix_' + prefix + '_search_image_time', table.c.search_image_file_id, table.c.captured_at, table.c.id)
        keyword = 'search_keyword' if name == 'collection_batches' else 'keyword'
        if name == 'collection_batches':
            table.append_column(sa.Column('search_keyword', sa.String(100), nullable=True,
                comment='来源搜索词：实际提交的原文；新关键词搜索必填，纯图片搜索或来源未知时为空，不从商品标题推测'))
            table.append_column(sa.Column('page_number', sa.Integer(), nullable=True,
                comment='来源列表实际页码：从1开始；原记录未提供或不适用时为空，不默认第一页'))
            table.append_column(sa.Column('search_options', mysql.JSON(none_as_null=True), nullable=True,
                comment='实际附加搜索条件：JSON对象，保存已确认的筛选或未来图片裁剪参数；SQL NULL表示未知或未提供',
                info={'json_contract': '来源条件版本1：对象保留实际价格、类目、地区等条件；未知为SQL NULL；未来裁剪参数需标明单位和来源图，不以空对象猜测无筛选'}))
            table.append_constraint(sa.CheckConstraint('page_number IS NULL OR page_number >= 1', name='ck_batch_search_page'))
            sa.Index('ix_batch_search_keyword_time', table.c.search_keyword, table.c.captured_at, table.c.id)
        table.append_constraint(sa.CheckConstraint(
            "search_method IS NULL OR search_method <> 'keyword' OR (" + keyword + " IS NOT NULL AND " + keyword + " <> '')",
            name='ck_' + prefix + '_search_keyword'))
        sa.Index('ix_' + prefix + '_search_method_keyword_time', table.c.search_method, table.c[keyword], table.c.captured_at, table.c.id)
    return metadata
