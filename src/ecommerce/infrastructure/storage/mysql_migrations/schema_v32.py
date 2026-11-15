"""第三十二版冻结模型，关联发布类目扫描与千牛任务。"""

import sqlalchemy as sa

from .schema_v31 import build_metadata as previous_metadata

REVISION = '0032_qianniu_scan_scope'
PREVIOUS_REVISION = '0031_listing_account_scope'
SCOPE_COMMENTS = {
    'account_id': '千牛任务选定的账号，未登记历史或其他平台任务为空',
    'shop_id': '千牛任务选定的店铺，未登记历史或其他平台任务为空',
}
SCAN_TASK_COMMENT = '执行本次扫描的千牛任务编号，未关联的历史扫描为空'


def build_metadata():
    metadata = previous_metadata()
    for field, comment in SCOPE_COMMENTS.items():
        metadata.tables['tasks'].c[field].comment = comment
    scans = metadata.tables['publish_category_scans']
    scans.append_column(sa.Column('task_id', sa.String(32, collation='ascii_bin'),
                                  comment=SCAN_TASK_COMMENT))
    scans.append_constraint(sa.UniqueConstraint('task_id', name='uq_publish_scan_task'))
    scans.append_constraint(sa.ForeignKeyConstraint(['task_id'], ['tasks.id'],
        name='fk_publish_scan_task', onupdate='RESTRICT', ondelete='RESTRICT'))
    return metadata
