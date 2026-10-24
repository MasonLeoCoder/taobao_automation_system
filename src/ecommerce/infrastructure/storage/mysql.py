"""MySQL 连接、显式迁移与实际结构核对；任务和采集业务分别按阶段接入。"""
import logging
import os
import re
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

from ecommerce.infrastructure.storage.mysql_migrations.schema_v5 import REVISION, PREVIOUS_REVISION, build_metadata
from ecommerce.infrastructure.storage.mysql_checks import check_expression_tree
from ecommerce.shared.config import MySQLSettings

log = logging.getLogger(__name__)


class MySQLStorageError(RuntimeError):
    """不暴露连接密码、SQL 参数或业务正文的数据库错误。"""


def safe_error(error):
    """生成安全错误说明；参数：异常；返回：中文摘要。"""
    original = getattr(error, "orig", error)
    args = getattr(original, "args", ())
    code = args[0] if args and isinstance(args[0], int) else None
    meanings = {1045:"登录被拒绝，请检查账号与密码环境变量",1044:"账号没有目标库访问权限",1049:"目标数据库不存在",
                1142:"账号不具备此操作权限",2003:"无法连接本机 MySQL，请检查服务与端口",2013:"数据库连接中断",
                2006:"数据库连接已失效",1062:"记录与现有唯一键冲突",1452:"关联的父记录不存在或归属不匹配",
                1451:"记录仍被其他数据引用",3819:"记录不满足数据库检查约束"}
    return meanings.get(code,"数据库操作失败，请检查连接、权限或结构版本")+(f"（错误码 {code}）" if code else "")


def create_mysql_engine(settings: MySQLSettings, *, migration=False):
    """建立小容量连接池；参数：本机配置、是否使用迁移账号；返回：Engine。"""
    env_name = settings.migration_password_env if migration else settings.password_env
    password = os.environ.get(env_name)
    if not password:
        raise MySQLStorageError(f"未设置密码环境变量 {env_name}；请在配置环境变量后的新终端运行")
    url = URL.create("mysql+pymysql",username=settings.migration_user if migration else settings.user,
                     password=password,host=settings.host,port=settings.port,database=settings.database,query={"charset":"utf8mb4"})
    engine = sa.create_engine(url,pool_size=2,max_overflow=1,pool_pre_ping=True,pool_recycle=1800,
        hide_parameters=True,connect_args={"connect_timeout":settings.connect_timeout,"read_timeout":10,"write_timeout":10})

    @sa.event.listens_for(engine,"connect")
    def configure_session(connection, record):
        """统一会话时区及严格写入；参数：DBAPI 连接、池记录；返回：无。"""
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION time_zone = '+08:00'")
            cursor.execute("SELECT @@SESSION.sql_mode")
            modes = set(cursor.fetchone()[0].split(','))
            modes.update({"STRICT_TRANS_TABLES","NO_ZERO_DATE","NO_ZERO_IN_DATE","ERROR_FOR_DIVISION_BY_ZERO","NO_ENGINE_SUBSTITUTION"})
            cursor.execute("SET SESSION sql_mode = %s",(','.join(sorted(modes)),))
    return engine


def version_table():
    """定义带中文备注的迁移版本表；参数：无；返回：Table。"""
    return sa.Table("alembic_version",sa.MetaData(),
        sa.Column("version_num",sa.String(32,collation="ascii_bin"),nullable=False,comment="当前结构版本：已成功执行的 Alembic 迁移编号"),
        sa.PrimaryKeyConstraint("version_num",name="alembic_version_pkc"),
        comment="数据库迁移版本表：记录已完成的结构升级版本，无业务外键",
        mysql_engine="InnoDB",mysql_charset="utf8mb4",mysql_collate="utf8mb4_0900_bin")


def connection_status(connection):
    """读取本机库连接信息；参数：连接；返回：不含凭据的状态字典。"""
    row = connection.execute(sa.text("SELECT VERSION() AS version,CURRENT_USER() AS account,DATABASE() AS database_name,@@SESSION.time_zone AS time_zone")).mappings().one()
    value = dict(row)
    match = re.match(r"(\d+)\.(\d+)",value['version'])
    if not match or 'MariaDB' in value['version'] or tuple(map(int,match.groups())) < (8,4):
        raise MySQLStorageError("当前结构要求 MySQL 8.4 或更新版本")
    return value


def current_revision(connection):
    """读取版本，不创建任何表；参数：连接；返回：版本编号或空值。"""
    if not sa.inspect(connection).has_table("alembic_version"):
        return None
    rows = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(rows)>1:
        raise MySQLStorageError("数据库存在多个结构分支，请先核对迁移版本")
    return rows[0] if rows else None


def upgrade_schema(settings):
    """显式执行迁移；参数：本机配置；返回：版本，不自动修补半完成结构。"""
    engine = create_mysql_engine(settings,migration=True)
    try:
        with engine.connect() as connection:
            connection_status(connection)
            lock_name = "taobao_schema_"+settings.database
            acquired = connection.execute(sa.text("SELECT GET_LOCK(:name,5)"),{"name":lock_name}).scalar_one()
            connection.commit()
            if acquired!=1:
                raise MySQLStorageError("另一个进程正在迁移数据库，请等待其结束")
            try:
                revision = current_revision(connection)
                tables = set(sa.inspect(connection).get_table_names())
                if revision not in (None,'0001_mysql_foundation','0002_beijing_time','0003_search_origin',PREVIOUS_REVISION,REVISION):
                    raise MySQLStorageError("数据库结构版本高于或不兼容当前程序，未执行迁移")
                if revision is None and tables-{"alembic_version"}:
                    raise MySQLStorageError("数据库存在未完成迁移或非本项目表，请核对结构；未删表、未自动盖章")
                connection.commit()
                config = Config()
                config.set_main_option("script_location",str(Path(__file__).parent/"mysql_migrations"))
                config.attributes['connection'] = connection
                command.upgrade(config,"head")
                connection.commit()
                return current_revision(connection)
            finally:
                if connection.in_transaction():
                    connection.rollback()
                connection.execute(sa.text("SELECT RELEASE_LOCK(:name)"),{"name":lock_name})
                connection.commit()
    except sa.exc.SQLAlchemyError as error:
        message = safe_error(error)
        log.error("MySQL 结构迁移失败：%s",message)
        raise MySQLStorageError(message) from None
    finally:
        engine.dispose()


def _normalize_type(value):
    """规范反射后的类型拼写；参数：类型文本；返回：可比较文本。"""
    value = re.sub(r'\s+CHARACTER SET\s+\w+','',value,flags=re.I)
    result = re.split(r'\s+COLLATE\s+',value,flags=re.I)[0].lower().replace(' ','')
    return 'tinyint(1)' if result=='bool' or result=='boolean' else result


def inspect_schema(settings):
    """只读核对实际结构；参数：本机配置；返回：连接、备注、字段和约束报告。"""
    engine = create_mysql_engine(settings)
    try:
        with engine.connect() as connection:
            status = connection_status(connection)
            inspector = sa.inspect(connection)
            expected = build_metadata()
            version = version_table()
            expected_tables = dict(expected.tables,alembic_version=version)
            actual_names = set(inspector.get_table_names())
            problems=[]
            if actual_names!=set(expected_tables):
                problems.append("表清单不一致：缺少 "+','.join(sorted(set(expected_tables)-actual_names))+"；额外 "+','.join(sorted(actual_names-set(expected_tables))))
            enforced={(r[0],r[1]):r[2] for r in connection.execute(sa.text("SELECT TABLE_NAME,CONSTRAINT_NAME,ENFORCED FROM information_schema.TABLE_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() AND CONSTRAINT_TYPE='CHECK'"))}
            detail={}
            for name,table in expected_tables.items():
                if name not in actual_names:
                    continue
                columns = inspector.get_columns(name)
                actual_columns = {c['name']:c for c in columns}
                comment = inspector.get_table_comment(name)['text']
                options = inspector.get_table_options(name)
                if options.get('mysql_engine')!='InnoDB' or options.get('mysql_collate')!='utf8mb4_0900_bin':
                    problems.append(f"{name} 存储引擎或默认字符排序规则不一致")
                if comment!=table.comment:
                    problems.append(f"{name} 表备注不一致")
                if set(actual_columns)!=set(table.columns.keys()):
                    problems.append(f"{name} 字段清单不一致")
                for column in table.columns:
                    actual = actual_columns.get(column.name)
                    if not actual:
                        continue
                    if actual.get('comment')!=column.comment:
                        problems.append(f"{name}.{column.name} 字段备注不一致")
                    if bool(actual['nullable'])!=column.nullable:
                        problems.append(f"{name}.{column.name} 可空性不一致")
                    if _normalize_type(str(actual['type'].compile(dialect=engine.dialect)))!=_normalize_type(str(column.type.compile(dialect=engine.dialect))):
                        problems.append(f"{name}.{column.name} 字段类型不一致")
                    expected_default = str(column.server_default.arg).strip("'\"") if column.server_default is not None else None
                    actual_default = str(actual['default']).strip("'\"") if actual['default'] is not None else None
                    if expected_default!=actual_default:
                        problems.append(f"{name}.{column.name} 默认值不一致")
                    if getattr(column.type,'collation',None) and getattr(actual['type'],'collation',None)!=column.type.collation:
                        problems.append(f"{name}.{column.name} 标识排序规则不一致")
                primary = inspector.get_pk_constraint(name)
                if primary['constrained_columns']!=list(table.primary_key.columns.keys()):
                    problems.append(f"{name} 主键不一致")
                foreign = inspector.get_foreign_keys(name)
                actual_foreign = {f['name']:f for f in foreign}
                expected_foreign = {f.name:f for f in table.foreign_key_constraints}
                if set(actual_foreign)!=set(expected_foreign):
                    problems.append(f"{name} 外键清单不一致")
                for key,constraint in expected_foreign.items():
                    actual=actual_foreign.get(key)
                    if not actual:
                        continue
                    if actual['constrained_columns']!=[e.parent.name for e in constraint.elements] or actual['referred_table']!=constraint.referred_table.name or actual['referred_columns']!=[e.column.name for e in constraint.elements]:
                        problems.append(f"{name}.{key} 外键目标不一致")
                    for action in ('ondelete','onupdate'):
                        if actual.get('options',{}).get(action,'RESTRICT').upper() not in {'RESTRICT','NO ACTION'}:
                            problems.append(f"{name}.{key} 外键更新删除规则不一致")
                actual_unique={u['name']:u['column_names'] for u in inspector.get_unique_constraints(name)}
                expected_unique={u.name:list(u.columns.keys()) for u in table.constraints if isinstance(u,sa.UniqueConstraint)}
                if actual_unique!=expected_unique:
                    problems.append(f"{name} 唯一约束不一致")
                actual_checks={c['name']:c['sqltext'] for c in inspector.get_check_constraints(name)}
                expected_checks={c.name:str(c.sqltext) for c in table.constraints if isinstance(c,sa.CheckConstraint)}
                if set(actual_checks)!=set(expected_checks):
                    problems.append(f"{name} 检查约束清单不一致")
                for key,expression in expected_checks.items():
                    if key in actual_checks:
                        if enforced.get((name,key))!='YES':
                            problems.append(f"{name}.{key} 检查约束未启用")
                        try:
                            same=check_expression_tree(expression)==check_expression_tree(actual_checks[key])
                        except ValueError:
                            same=False
                        if not same:
                            problems.append(f"{name}.{key} 检查约束表达式不一致或无法可靠比较")
                actual_indexes={i['name']:i for i in inspector.get_indexes(name)}
                for index in table.indexes:
                    actual = actual_indexes.get(index.name)
                    if not actual or actual['column_names']!=list(index.columns.keys()) or bool(actual['unique'])!=index.unique:
                        problems.append(f"{name}.{index.name} 查询索引不一致")
                detail[name]={'comment':comment,'columns':[{"name":c['name'],"type":str(c['type']),"nullable":c['nullable'],"default":c['default'],"comment":c.get('comment')} for c in columns],
                              'table_options':options,
                              'primary_key':primary['constrained_columns'],'foreign_keys':foreign,'unique_constraints':actual_unique,'check_constraints':actual_checks,
                              'indexes':[{"name":i['name'],"columns":i['column_names'],"unique":bool(i['unique'])} for i in actual_indexes.values()]}
            revision = current_revision(connection)
            if revision!=REVISION:
                problems.append("结构版本尚未达到当前程序要求")
            grants=connection.execute(sa.text("SHOW GRANTS FOR CURRENT_USER")).scalars().all()
            return {**status,'revision':revision,'expected_revision':REVISION,'business_table_count':len(expected.tables),
                    'table_count':len(actual_names),'column_count':sum(len(t['columns']) for t in detail.values()),
                    'foreign_key_count':sum(len(t['foreign_keys']) for t in detail.values()),
                    'all_comments_present':set(expected_tables)<=actual_names and all(t['comment'] and all(c['comment'] for c in t['columns']) for t in detail.values()),
                    'schema_errors':problems,'grants':grants,'tables':detail}
    except sa.exc.SQLAlchemyError as error:
        message=safe_error(error)
        log.error("MySQL 结构检查失败：%s",message)
        raise MySQLStorageError(message) from None
    finally:
        engine.dispose()
