"""第二十五版冻结模型：Web菜单配置，不改变已有采集与经营资料。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from ecommerce.infrastructure.storage.mysql_migrations.schema_v24 import build_metadata as previous_metadata

REVISION = '0025_web_menu'
PREVIOUS_REVISION = '0024_cross_analysis'
NEW_TABLES = ('web_menu_groups', 'web_menu_items')


def build_metadata():
    """构造菜单配置模型；参数：无；返回：含两个配置表的完整元数据。"""
    metadata = previous_metadata()
    common = dict(mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_0900_bin')
    for name, key, comment in (
        ('web_menu_groups', 'group_key', 'Web菜单分组配置：固定标识关联程序图标，名称、排序、启用及显示由本机用户维护'),
        ('web_menu_items', 'menu_key', 'Web菜单项目配置：固定功能标识关联程序路由；本表控制展示和Web启停，不生成新业务功能'),
    ):
        columns = [
            sa.Column(key, sa.String(64, collation='ascii_bin'), primary_key=True,
                      comment='固定'+('分组' if key == 'group_key' else '功能')+'标识：迁移初始化，日常不可改名；不是中文显示名称'),
            sa.Column('name', sa.String(80), nullable=False, comment='中文显示名称：1至80字普通文字；修改后后续Web请求生效'),
            sa.Column('sort_order', sa.Integer, nullable=False, server_default='0', comment='显示顺序：非负整数，越小越靠前；同值按固定标识升序'),
            sa.Column('is_enabled', sa.Boolean, nullable=False, server_default='1', comment='是否启用：1启用、0停用；分组停用优先于子项，限制Web页面和新操作，不终止已有任务或影响CLI'),
            sa.Column('is_visible', sa.Boolean, nullable=False, server_default='1', comment='是否在Web菜单显示：1显示、0隐藏；隐藏不限制启用功能的直接链接；分组隐藏优先于子项'),
            sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP(6)'),
                      comment='配置创建时间：北京时间UTC+08:00；数据库默认生成，手动SQL会话须使用+08:00'),
            sa.Column('updated_at', mysql.DATETIME(fsp=6), nullable=False,
                      server_default=sa.text('CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)'),
                      comment='配置最近修改时间：北京时间UTC+08:00；数据库自动更新，手动SQL会话须使用+08:00'),
        ]
        constraints = [sa.CheckConstraint('sort_order >= 0', name='ck_'+name+'_sort'),
                       sa.CheckConstraint('is_enabled IN (0,1)', name='ck_'+name+'_enabled'),
                       sa.CheckConstraint('is_visible IN (0,1)', name='ck_'+name+'_visible')]
        if key == 'menu_key':
            columns += [
                sa.Column('group_key', sa.String(64, collation='ascii_bin'), nullable=False, comment='所属分组：关联web_menu_groups.group_key；允许移动到已登记分组，不改变功能路由'),
                sa.Column('construction_status', sa.String(16, collation='ascii_bin'), nullable=False,
                          comment='建设状态：available已接入、planned待建设；由对应版本迁移维护，须与程序真实能力一致，不是启用开关'),
            ]
            constraints += [sa.ForeignKeyConstraint(['group_key'], ['web_menu_groups.group_key'],
                                name='fk_web_menu_item_group', onupdate='RESTRICT', ondelete='RESTRICT'),
                            sa.CheckConstraint("construction_status IN ('available','planned')", name='ck_web_menu_items_construction')]
        table = sa.Table(name, metadata, *columns, *constraints, comment=comment, **common)
        sa.Index('ix_'+name+'_order', *([table.c.group_key] if key == 'menu_key' else []), table.c.sort_order, table.c[key])
    return metadata
