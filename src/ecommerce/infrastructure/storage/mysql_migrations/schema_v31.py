"""第三十一版冻结模型，保存上架任务的账号及店铺归属。"""

import sqlalchemy as sa

from .schema_v30 import build_metadata as previous_metadata

REVISION = '0031_listing_account_scope'
PREVIOUS_REVISION = '0030_account_lifecycle'
OWNERS = {
    'account_id': ('platform_accounts', '上架任务选定的千牛账号，历史未登记时为空'),
    'shop_id': ('shops', '上架任务选定的目标店铺，历史未登记时为空'),
}


def build_metadata():
    metadata = previous_metadata()
    table = metadata.tables['tasks']
    for field, (target, comment) in OWNERS.items():
        table.append_column(sa.Column(field, sa.String(32, collation='ascii_bin'), comment=comment))
        table.append_constraint(sa.ForeignKeyConstraint([field], [target + '.id'],
            name='fk_tasks_' + field, onupdate='RESTRICT', ondelete='RESTRICT'))
        sa.Index('ix_tasks_' + field, table.c[field])
    return metadata
