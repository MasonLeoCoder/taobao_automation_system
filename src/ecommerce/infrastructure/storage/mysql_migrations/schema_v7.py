"""第七版冻结模型：基本信息独立于规格，保留旧批次及来源。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from ecommerce.infrastructure.storage.mysql_migrations.schema_v6 import build_metadata as previous_metadata

REVISION='0007_basic_info'
PREVIOUS_REVISION='0006_list_sales'
MODULES=('basic_info','video','main_images','skus','parameters','detail_images','review_summary','reviews','questions')


def build_metadata():
    """构造九模块模型；参数：无；返回：含中文备注、归属约束的元数据。"""
    metadata=previous_metadata()
    batch=metadata.tables['collection_batches']
    current=metadata.tables['product_current_sections']
    for table,name,expression in [
        (batch,'ck_batch_module',"module IN ('list',"+','.join("'"+m+"'" for m in MODULES)+')'),
        (current,'ck_current_section_module',"(section='product_info' AND module='basic_info') OR (section='sku_details' AND module='skus') OR (section=module AND section IN ('video','main_images','parameters','detail_images','review_summary','reviews','questions'))")]:
        old=next(c for c in table.constraints if c.name==name)
        table.constraints.remove(old)
        table.append_constraint(sa.CheckConstraint(expression,name=name))
    batch.c.module.comment='模块名称：list 或 basic_info、video、main_images、skus、parameters、detail_images、review_summary、reviews、questions 九个详情模块'
    metadata.tables['collection_steps'].c.module.comment='模块名称：basic_info、video、main_images、skus、parameters、detail_images、review_summary、reviews、questions；旧运行仍保留原八步'
    current.c.section.comment='当前分区名称：product_info 对应 basic_info；sku_details 对应 skus；其他分区与模块同名'
    batch.append_constraint(sa.UniqueConstraint('id','product_id','module',name='uq_batch_product_module'))
    number=lambda:mysql.BIGINT(unsigned=True)
    info=sa.Table('product_info_observations',metadata,
        sa.Column('id',number(),primary_key=True,autoincrement=True,comment='内部编号：数据库自增主键，不代替平台商品编号'),
        sa.Column('batch_id',number(),nullable=False,comment='基本信息批次编号：联合 product_id、module 关联 collection_batches 的同名归属，每批一条'),
        sa.Column('product_id',number(),nullable=False,comment='商品内部编号：由所属基本信息批次约束到同一 products.id，不是平台商品编号'),
        sa.Column('module',sa.String(32,collation='ascii_bin'),nullable=False,comment='固定模块名称：basic_info，由数据库约束核对'),
        sa.Column('title',sa.Text(),nullable=True,comment='详情标题原文：仅来自当前详情或旧SKU内实际保存的基本信息，未确认时为空'),
        sa.Column('shop_name',sa.Text(),nullable=True,comment='详情店铺名称：页面明确展示的原文，未提供或不唯一时为空，不从列表补齐'),
        sa.Column('sales_text',sa.Text(),nullable=True,comment='详情销量展示原文：保留已售及加号等口径，未展示或不唯一时为空，不代表精确销量'),
        sa.Column('price_text',sa.Text(),nullable=True,comment='页面展示价格原文：保留起价、区间和优惠说明，不替代某规格实际价格，未确认时为空'),
        sa.Column('source_url',sa.Text(),nullable=True,comment='详情来源链接：经过商品身份校验的规范化地址，历史缺失时为空，不猜测补填'),
        sa.Column('captured_at',mysql.DATETIME(fsp=6),nullable=True,comment='基本信息实际观察时间：北京时间（UTC+08:00），微秒精度；历史未记录时为空，不使用迁移时间'),
        sa.Column('field_states',mysql.JSON(),nullable=False,comment='基本信息字段状态：JSON对象，provided已读取、not_displayed未展示、ambiguous不唯一、not_recorded历史未记录',info={'json_contract':'title、shop_name、sales_text、price_text分别记录provided/not_displayed/ambiguous/not_recorded；不以空值冒充零'}),
        sa.Column('payload',mysql.JSON(),nullable=True,comment='原基本信息及来源扩展：JSON对象，包含原文、缺失提示及迁移来源，不以缺失字段清除旧有效资料',info={'json_contract':'基本信息原文、field_states、captured_at及选择器依据；迁移来源登记于所属批次result_payload.record.migration_source'}),
        sa.UniqueConstraint('batch_id',name='uq_product_info_batch'),
        sa.CheckConstraint("module='basic_info'",name='ck_product_info_module'),
        sa.ForeignKeyConstraint(['batch_id','product_id','module'],['collection_batches.id','collection_batches.product_id','collection_batches.module'],
                                name='fk_product_info_batch_owner',ondelete='RESTRICT',onupdate='RESTRICT'),
        comment='商品基本信息观察表：独立保存每次详情标题、店铺、销量及展示价格，不依赖SKU是否采集成功',
        mysql_engine='InnoDB',mysql_charset='utf8mb4',mysql_collate='utf8mb4_0900_bin')
    sa.Index('ix_product_info_product_time',info.c.product_id,info.c.captured_at,info.c.id)
    return metadata
