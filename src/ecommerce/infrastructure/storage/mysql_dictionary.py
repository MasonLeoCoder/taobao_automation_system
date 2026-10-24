"""根据同版迁移结构生成完整中文字段字典，避免手工遗漏字段。"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from ecommerce.infrastructure.storage.mysql import version_table
from ecommerce.infrastructure.storage.mysql_migrations.schema_v3 import REVISION,build_metadata


def render_dictionary():
    """生成全字段及约束说明；参数：无；返回：Markdown 字符串。"""
    metadata=build_metadata()
    tables=list(metadata.tables.values())+[version_table()]
    lines=['# 数据库完整字段字典','',f'结构版本：`{REVISION}`。业务表 22 张，技术版本表 1 张。',
           '', '本字典由同版迁移定义生成；数据库实际结构由 `database check` 核对。关系总览见 [数据库表关系说明](数据库表关系说明.md)。',
           '', '字段备注写入数据库原生 COMMENT。所有 DATETIME 字段直接存北京时间（UTC+08:00），查询返回带 +08:00 的 ISO 时间，界面不重复加八小时；来源原文 time_raw 和 JSON/文件保留原始时区。空值与 0、空字符串、确认无数据分别解释。',
           '', '采集 JSON 扩展按来源 schema_version 保留业务字段，原始文件另行保存；任务事件、导入 JSON 键与版本口径见 [任务与日志结构口径](数据库表关系说明.md#8-已接入的任务与日志结构口径)，当前业务取值查 [字段取值说明](数据库字段取值说明.md)，早期设计见 [历史接入设计](历史记录/MySQL接入与历史迁移设计.md)。','']

    def cell(value):
        """转义 Markdown 单元格；参数：值；返回：安全文本。"""
        return str(value).replace('|','\\|').replace('\n',' ')

    for table in sorted(tables,key=lambda t:t.name):
        lines += [f'## {table.name}', '', f'表备注：{table.comment}', '', '| 字段 | 中文名称与用途（数据库实际备注） | 类型/精度 | 可空 | 默认值 | 主键/关联 |', '|---|---|---|---|---|---|']
        for column in table.columns:
            associations=['主键'] if column.primary_key else []
            associations += [f'{fk.name} → {element.target_fullname}' for fk in sorted(table.foreign_key_constraints,key=lambda item:item.name) for element in fk.elements if element.parent.name==column.name]
            default=str(column.server_default.arg) if column.server_default is not None else ('自增' if column.primary_key and column.autoincrement is True else '无')
            lines.append('| '+' | '.join(map(cell,[f'`{column.name}`',column.comment,column.type.compile(dialect=mysql.dialect()),'是' if column.nullable else '否',default,'；'.join(associations) or '—']))+' |')
        for column in table.columns:
            if column.info.get('json_contract'):
                lines += ['',f'- `{column.name}` JSON 口径：{column.info["json_contract"]}。']
        unique=[c for c in table.constraints if isinstance(c,sa.UniqueConstraint)]
        checks=[c for c in table.constraints if isinstance(c,sa.CheckConstraint)]
        foreign=sorted(table.foreign_key_constraints,key=lambda c:c.name)
        lines += ['', '约束与索引：', '']
        lines.append('- 主键：`'+', '.join(table.primary_key.columns.keys())+'`。')
        lines += ['- 唯一约束 `'+c.name+'`：`'+', '.join(c.columns.keys())+'`。' for c in sorted(unique,key=lambda c:c.name)]
        lines += ['- 外键 `'+c.name+'`：`'+', '.join(e.parent.name for e in c.elements)+'` → `'+c.referred_table.name+'('+', '.join(e.column.name for e in c.elements)+')`；更新/删除均 RESTRICT。' for c in foreign]
        lines += ['- 检查约束 `'+c.name+'`：`'+str(c.sqltext)+'`。' for c in sorted(checks,key=lambda c:c.name)]
        lines += ['- 查询索引 `'+i.name+'`：`'+', '.join(i.columns.keys())+'`。' for i in sorted(table.indexes,key=lambda i:i.name)]
        lines += ['']
    lines += ['## 需要业务事务校验的关系','',
              '- 当前指针还需核对批次已发布且满足业务完整性；数据库外键不能读取另一表的状态做条件判断。',
              '- 商品标题来自列表时需核对列表确实包含该商品；原始观察时间、人工确认时间与图片复用来源分别处理。',
              '- 派生批次的同商品/模块、自引用及循环、文件路径规范化哈希碰撞、跨源内容冲突由写入服务核对。',
              '- 导入目标表名和主键属于受控逻辑关联；正式查询还需核对导入提交状态和批次发布状态。',
              '- 任务、日志和采集明细已接入；写入服务落实归属、派生无环、当前分区完整性、导入提交过滤及逐字段核对。',
              '- 采集扩展 JSON、范围及发布依据见 [采集数据结构口径](数据库表关系说明.md#9-采集数据结构口径)；图片、视频和证据文件保留原件，数据库保存索引。','']
    return '\n'.join(lines)
