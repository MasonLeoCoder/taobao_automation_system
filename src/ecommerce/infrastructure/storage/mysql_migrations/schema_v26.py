"""第二十六版冻结模型：手动经营账本三表。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from ecommerce.infrastructure.storage.mysql_migrations.schema_v25 import build_metadata as previous_metadata

REVISION = '0026_ledger'
PREVIOUS_REVISION = '0025_web_menu'
NEW_TABLES = ('ledger_categories', 'ledger_entries', 'ledger_monthly_limits')


def build_metadata():
    """构造完整模型；参数：无；返回：含账本三表的元数据。"""
    metadata = previous_metadata()
    common = dict(mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_bin')

    def times():
        """生成系统时间列；参数：无；返回：两个列定义。"""
        return [sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False,
                          comment='创建时间：系统记录北京时间UTC+08:00，不用于账目月份归属'),
                sa.Column('updated_at', mysql.DATETIME(fsp=6), nullable=False,
                          comment='最后修改时间：系统记录北京时间UTC+08:00，不保留逐次修改历史')]

    sa.Table('ledger_categories', metadata,
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True, comment='分类主键：数据库自增整数，明细通过外键引用'),
        sa.Column('name', sa.String(80), nullable=False, comment='分类名称：去首尾空白后1至80字普通文字，唯一；改名同步展示历史明细'),
        sa.Column('sort_order', sa.Integer, nullable=False, comment='排序：0至2147483647整数，升序，同值按主键升序'),
        sa.Column('is_enabled', sa.Boolean, nullable=False, comment='是否启用：1可用于新账目，0停用；原明细保留原分类'),
        *times(), sa.UniqueConstraint('name', name='uq_ledger_category_name'),
        sa.CheckConstraint('sort_order >= 0', name='ck_ledger_category_sort'),
        sa.CheckConstraint('is_enabled IN (0,1)', name='ck_ledger_category_enabled'),
        comment='经营账本分类：用户管理名称排序启停，收入支出方向由明细决定', **common)
    entry = sa.Table('ledger_entries', metadata,
        sa.Column('id', sa.String(32, collation='ascii_bin'), primary_key=True, comment='账目主键：页面生成UUID十六进制32字符；同一表单重复提交幂等'),
        sa.Column('occurred_at', mysql.DATETIME(fsp=6), nullable=False, comment='账目发生时间：用户手动填写的北京时间UTC+08:00，决定统计月份，与录入时间独立'),
        sa.Column('direction', sa.String(8, collation='ascii_bin'), nullable=False, comment='收支类型：expense支出、income收入；正数金额按本字段归入月度统计'),
        sa.Column('category_id', sa.Integer, nullable=False, comment='分类主键：外键关联ledger_categories.id，非空，停用不影响原记录'),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False, comment='金额：人民币元，大于0且最多两位小数，上限9999999999.99；收入不抵扣止损额度'),
        sa.Column('note', sa.String(500), nullable=False, comment='备注：用户普通文字，0至500字；无备注保存空字符串'),
        sa.Column('status', sa.String(8, collation='ascii_bin'), nullable=False, comment='状态：active正常参与统计、void作废不参与；作废记录不再修改'),
        sa.Column('revision', sa.Integer, nullable=False, comment='并发版本：新建1，每次修改或作废加1，旧页面禁止覆盖新值'),
        *times(), sa.ForeignKeyConstraint(['category_id'], ['ledger_categories.id'], name='fk_ledger_entry_category', onupdate='RESTRICT', ondelete='RESTRICT'),
        sa.CheckConstraint("direction IN ('expense','income')", name='ck_ledger_entry_direction'),
        sa.CheckConstraint("status IN ('active','void')", name='ck_ledger_entry_status'),
        sa.CheckConstraint('amount > 0', name='ck_ledger_entry_amount'),
        sa.CheckConstraint('revision >= 1', name='ck_ledger_entry_revision'),
        comment='手动经营收支明细：按用户发生时间统计；系统创建修改时间独立；作废不统计', **common)
    sa.Index('ix_ledger_entry_month', entry.c.occurred_at, entry.c.id)
    sa.Index('ix_ledger_entry_category', entry.c.category_id)
    sa.Table('ledger_monthly_limits', metadata,
        sa.Column('month', sa.Date, primary_key=True, comment='预算月份主键：用户所选月份的第一日，如2026-11-01；程序强制规范为月初'),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False, comment='当月止损线：用户手动输入人民币元，0至9999999999.99且最多两位小数；无记录表示未设置'),
        *times(), sa.CheckConstraint('amount >= 0', name='ck_ledger_limit_amount'),
        comment='月度止损线：每月独立手动设置，不自动继承，不限制记账；通过发生月份逻辑对应明细', **common)
    return metadata
