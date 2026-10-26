"""基本信息独立为第九模块；保留旧SKU原文，按自身完整性提取已有信息。"""
from alembic import op
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql_migrations.schema_v7 import build_metadata
from ecommerce.infrastructure.storage.mysql_migrations.basic_info_v7 import split_legacy_info

revision='0007_basic_info'
down_revision='0006_list_sales'
branch_labels=None
depends_on=None


def upgrade():
    """新增表和模块约束并事务提取旧信息；参数：无；返回：无，失败可幂等重入。"""
    c=op.get_bind()
    if c.scalar(sa.text('SELECT COUNT(*) FROM tasks WHERE finished_at IS NULL')):
        raise RuntimeError('存在未结束任务，暂不拆分基本信息')
    metadata=build_metadata()
    with op.get_context().autocommit_block():
        b=metadata.tables['collection_batches']
        if 'uq_batch_product_module' not in {u['name'] for u in sa.inspect(c).get_unique_constraints(b.name)}:
            op.create_unique_constraint('uq_batch_product_module',b.name,['id','product_id','module'])
        # 新旧基本信息指针互斥；DDL中断后仍可依据旧SKU原文重建，不删除业务原件。
        for table,name in [('collection_batches','ck_batch_module'),('product_current_sections','ck_current_section_module')]:
            checks={check['name']:check['sqltext'] for check in sa.inspect(c).get_check_constraints(table)}
            if name in checks:
                op.drop_constraint(name,table,type_='check')
        for table,column in [('collection_batches','module'),('collection_steps','module'),('product_current_sections','section')]:
            model=metadata.tables[table].c[column]
            op.alter_column(table,column,existing_type=model.type,existing_nullable=model.nullable,comment=model.comment)
        if not sa.inspect(c).has_table('product_info_observations'):
            model=metadata.tables['product_info_observations']
            model.create(c)
        # 先取消旧SKU对基本信息的发布指针，SKU批次/文件/步骤原样保留。
        c.execute(sa.text("DELETE FROM product_current_sections WHERE section='product_info' AND module='skus'"))
        for table,name in [('collection_batches','ck_batch_module'),('product_current_sections','ck_current_section_module')]:
            expected=next(check for check in metadata.tables[table].constraints if check.name==name)
            op.create_check_constraint(name,table,str(expected.sqltext))
    split_legacy_info(c,metadata.tables)


def downgrade():
    """拒绝自动丢弃独立基本信息；参数：无；返回：异常。"""
    raise RuntimeError('基本信息已独立保存，回退须单独审核数据与来源')
