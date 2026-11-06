"""第二十八版冻结模型：生意参谋搜索排行快照。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from .schema_v27 import build_metadata as previous_metadata

REVISION = '0028_sycm_search'
PREVIOUS_REVISION = '0027_publish_categories'
NEW_TABLES = ('sycm_search_batches', 'sycm_search_batch_types', 'sycm_search_rank_entries')


def build_metadata():
    metadata = previous_metadata()
    common = dict(mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_bin')
    timestamp = mysql.DATETIME(fsp=6)
    uuid = sa.String(32, collation='ascii_bin')
    batches = sa.Table(NEW_TABLES[0], metadata,
        sa.Column('id', uuid, primary_key=True, comment='排行快照批次编号'),
        sa.Column('task_id', uuid, sa.ForeignKey('tasks.id', name='fk_sycm_batch_task'), nullable=False,
                  comment='执行采集的后台任务编号'),
        sa.Column('publish_category_id', sa.BigInteger, sa.ForeignKey('publish_categories.id', name='fk_sycm_batch_category'),
                  nullable=False, comment='用户选择的千牛发布类目本地编号'),
        sa.Column('publish_category_path', sa.String(750), nullable=False, comment='启动时的千牛完整类目路径'),
        sa.Column('sycm_category_path', sa.String(750), comment='页面确认的实际统计类目路径'),
        sa.Column('sycm_category_id', sa.String(64), comment='页面提供的统计类目编号，可空'),
        sa.Column('ranking_type', sa.String(24), nullable=False, comment='榜单类型，首版固定 popularity 搜索人气'),
        sa.Column('period_type', sa.String(8), nullable=False, comment='周期类型 day、7d 或 30d'),
        sa.Column('period_start', sa.Date, comment='页面确认的统计开始日期'),
        sa.Column('period_end', sa.Date, comment='页面确认的统计结束日期，日榜与开始日期相同'),
        sa.Column('requested_pages', sa.Integer, nullable=False, comment='每种词的请求页数，从第一页起'),
        sa.Column('status', sa.String(16), nullable=False, comment='批次状态 running、complete、partial 或 failed'),
        sa.Column('started_at', timestamp, nullable=False, comment='采集开始时间，北京时间'),
        sa.Column('finished_at', timestamp, comment='采集结束时间，北京时间'),
        sa.UniqueConstraint('task_id', name='uq_sycm_batch_task'),
        comment='生意参谋搜索排行采集批次', **common)
    sa.Index('ix_sycm_batch_category_started', batches.c.publish_category_id, batches.c.started_at, batches.c.id)
    types = sa.Table(NEW_TABLES[1], metadata,
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True, comment='词类型结果本地编号'),
        sa.Column('batch_id', uuid, sa.ForeignKey('sycm_search_batches.id', name='fk_sycm_type_batch'), nullable=False,
                  comment='所属采集批次'),
        sa.Column('word_type', sa.String(16), nullable=False, comment='search搜索词、trend趋势词、core核心词、modifier修饰词'),
        sa.Column('collected_pages', sa.Integer, nullable=False, comment='完整读取并保存的页数'),
        sa.Column('row_count', sa.Integer, nullable=False, comment='当前词类型的明细行数'),
        sa.Column('status', sa.String(16), nullable=False, comment='pending、complete、partial、failed 或 changed'),
        sa.Column('end_reason', sa.String(24), comment='requested采满、end末页、empty空榜、changed榜单变化、error失败或 interrupted中断'),
        sa.Column('failed_page', sa.Integer, comment='可定位的失败页码，正常或未知时为空'),
        sa.Column('error_message', sa.String(500), comment='可安全展示的失败或变化原因'),
        sa.UniqueConstraint('batch_id', 'word_type', name='uq_sycm_batch_word_type'),
        comment='每批各词类型的采集进度与结果', **common)
    sa.Table(NEW_TABLES[2], metadata,
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True, comment='排行明细本地编号'),
        sa.Column('batch_type_id', sa.BigInteger, sa.ForeignKey(types.c.id, name='fk_sycm_entry_type'), nullable=False,
                  comment='所属批次词类型结果'),
        sa.Column('page_no', sa.Integer, nullable=False, comment='榜单页码，从一开始'),
        sa.Column('row_no', sa.Integer, nullable=False, comment='页内行号，从一开始'),
        sa.Column('rank', sa.Integer, nullable=False, comment='页面实际展示的排名，奖牌读为数字'),
        sa.Column('keyword', sa.String(500), nullable=False, comment='页面词原文'),
        sa.Column('popularity_raw', sa.String(128), comment='搜索人气原值，保留区间'),
        sa.Column('click_rate_raw', sa.String(128), comment='点击率原值，保留百分号'),
        sa.Column('payment_conversion_raw', sa.String(128), comment='支付转化率原值，保留区间'),
        sa.Column('extra_metrics', mysql.JSON, nullable=False, comment='其他页面表头与指标原值'),
        sa.Column('captured_at', timestamp, nullable=False, comment='所在页确认时间，北京时间'),
        sa.UniqueConstraint('batch_type_id', 'page_no', 'row_no', name='uq_sycm_entry_page_row'),
        comment='生意参谋排行词及页面指标快照', **common)
    return metadata
