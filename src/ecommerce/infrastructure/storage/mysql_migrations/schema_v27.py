"""第二十七版冻结模型：千牛发布类目与采集记录。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from .schema_v26 import build_metadata as previous_metadata

REVISION = '0027_publish_categories'
PREVIOUS_REVISION = '0026_ledger'
NEW_TABLES = ('publish_categories', 'publish_category_scans')


def build_metadata():
    metadata = previous_metadata()
    common = dict(mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_bin')
    timestamp = mysql.DATETIME(fsp=6)
    sa.Table('publish_categories', metadata,
        sa.Column('id', sa.BigInteger, primary_key=True, comment='本地类目编号，跨改名迁移保留'),
        sa.Column('parent_id', sa.BigInteger, sa.ForeignKey('publish_categories.id',
                  name='fk_publish_category_parent'), comment='父类目本地编号，一级留空'),
        sa.Column('qianniu_category_id', sa.String(64), comment='千牛平台类目编号，页面未提供时留空'),
        sa.Column('name', sa.String(255), nullable=False, comment='千牛类目名称原文'),
        sa.Column('full_path', sa.String(750), nullable=False, comment='完整类目路径，层级用大于号分隔'),
        sa.Column('level', sa.SmallInteger, nullable=False, comment='从一级开始的实际类目层级'),
        sa.Column('is_leaf', sa.Boolean, nullable=False, comment='是否为末级'),
        sa.Column('is_visible', sa.Boolean, nullable=False, comment='最近一次完整核对后当前账号是否可见'),
        sa.Column('listing_profile_key', sa.String(64), comment='代码中上架方案编号，未适配留空'),
        sa.Column('profile_status', sa.String(16), comment='verified已验证、review待复核，未绑定留空'),
        sa.Column('first_seen_at', timestamp, nullable=False, comment='首次采集时间，北京时间'),
        sa.Column('last_seen_at', timestamp, nullable=False, comment='最近一次确认可见时间，北京时间'),
        sa.UniqueConstraint('qianniu_category_id', name='qianniu_category_id'),
        sa.UniqueConstraint('full_path', name='full_path'),
        comment='千牛后台商品发布类目目录', **common)
    sa.Table('publish_category_scans', metadata,
        sa.Column('id', sa.String(32), primary_key=True, comment='本次采集编号'),
        sa.Column('scopes', mysql.JSON, nullable=False, comment='本次采集的分支路径，空数组代表全量'),
        sa.Column('status', sa.String(16), nullable=False, comment='running运行、complete完成、partial部分、failed失败'),
        sa.Column('result', mysql.JSON, nullable=False, comment='各分支完成情况、差异和待核对项'),
        sa.Column('started_at', timestamp, nullable=False, comment='采集开始时间，北京时间'),
        sa.Column('finished_at', timestamp, comment='结果确认时间，北京时间'),
        comment='千牛发布类目采集批次与分支审计', **common)
    return metadata
