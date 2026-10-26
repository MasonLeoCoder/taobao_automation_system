"""第六版冻结模型：列表销量结构化字段和组合约束。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from ecommerce.infrastructure.storage.mysql_migrations.schema_v5 import build_metadata as previous_metadata

REVISION = '0006_list_sales'
PREVIOUS_REVISION = '0005_remove_history_import'


def build_metadata():
    """构造第六版独立模型；参数：无；返回：含销量备注、约束和索引的模型。"""
    metadata = previous_metadata()
    listing = metadata.tables['list_observations']
    listing.append_column(sa.Column('sales_count', mysql.BIGINT(unsigned=True), nullable=True,
        comment='销量展示数量：精确展示、下界或近似值，结合sales_precision/metric/period解释；未知为NULL，不代表0'))
    for name, length, default, comment in (
        ('sales_precision', 16, 'unknown', '销量数量精度：exact精确展示、lower_bound下界、approximate近似、unknown未知；不代表核实的真实成交量'),
        ('sales_metric', 24, 'unknown', '销量统计口径：paid_people付款人数、received_people收货人数、sold_units已售件数、unknown未知'),
        ('sales_period', 24, 'unknown', '销量统计周期：cumulative明确累计、monthly月度、last_30_days近30天、unknown未说明或无法识别'),
        ('sales_parse_status', 24, 'pending', '销量解析状态：parsed已解析、no_quantity无数量、unrecognized无法识别、not_provided未提供、pending待解析')):
        listing.append_column(sa.Column(name, sa.String(length, collation='ascii_bin'), nullable=False,
                                        server_default=sa.text("'" + default + "'"), comment=comment))
    checks = {
        'ck_list_sales_precision': "sales_precision IN ('exact','lower_bound','approximate','unknown')",
        'ck_list_sales_metric': "sales_metric IN ('paid_people','received_people','sold_units','unknown')",
        'ck_list_sales_period': "sales_period IN ('cumulative','monthly','last_30_days','unknown')",
        'ck_list_sales_status': "sales_parse_status IN ('parsed','no_quantity','unrecognized','not_provided','pending')",
        'ck_list_sales_consistent': "(sales_parse_status = 'parsed' AND sales_count IS NOT NULL AND sales_precision <> 'unknown') OR (sales_parse_status <> 'parsed' AND sales_count IS NULL AND sales_precision = 'unknown')",
    }
    for name, expression in checks.items():
        listing.append_constraint(sa.CheckConstraint(expression, name=name))
    sa.Index('ix_list_sales_count', listing.c.sales_count, listing.c.id)
    return metadata
