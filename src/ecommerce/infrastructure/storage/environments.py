"""运行环境隔离和三库统一结构维护；业务记录不跨库复制。"""
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.shared.config import load_settings
from ecommerce.shared.time import BEIJING

DATABASES = {'taobao_automation': '正式', 'taobao_automation_test': '人工测试',
             'taobao_automation_verify': '自动验证'}
_maintenance_writer = ContextVar('maintenance_writer', default=False)


def label(settings):
    """返回公开环境名称；参数：应用配置；返回：中文标签，不含凭据。"""
    return DATABASES.get(settings.mysql.database, '独立环境') if settings.task_backend == 'mysql' else '本地文件'


def validate(settings):
    """核对已登记测试环境的所有可写路径；参数：配置；返回：原配置。"""
    expected_mode = {'taobao_automation':'production','taobao_automation_test':'test','taobao_automation_verify':'verify'}.get(settings.mysql.database,'')
    if settings.environment and settings.environment != expected_mode:
        raise ValueError('任务环境标识与数据库名称不匹配')
    if settings.mysql.database == 'taobao_automation' and settings.workspace.resolve() != (settings.root/'workspace').resolve():
        raise ValueError('正式环境必须使用workspace')
    if settings.mysql.database != 'taobao_automation_test':
        return settings
    expected = (settings.root / 'workspace_test').resolve()
    if settings.workspace.resolve() != expected or settings.task_backend != 'mysql' or settings.collection_backend != 'mysql':
        raise ValueError('人工测试库必须使用workspace_test和MySQL双后端')
    paths = [settings.session_path, settings.profile_path]
    paths.extend(path for path in (settings.qianniu_session_path,settings.qianniu_profile_path) if path)
    for name in ('taobao_session_path', 'taobao_profile_path'):
        paths.append(getattr(settings, name) or expected / ('storage/sessions/taobao.json' if name.endswith('session_path') else 'storage/browser_profiles/taobao'))
    for options in (settings.bilibili, settings.youtube):
        if options.get('temp_dir'):
            paths.append(Path(options['temp_dir']))
    if any(not Path(path).resolve().is_relative_to(expected) for path in paths):
        raise ValueError('人工测试的登录资料和临时目录必须位于workspace_test内')
    return settings


def state_path(mysql):
    """定位项目共用维护标记；参数：数据库引用；返回：路径或None。"""
    if mysql.database not in DATABASES or not mysql.credentials_file:
        return None
    reference = Path(mysql.credentials_file).resolve()
    # 正式/人工配置位于config；独立验证配置允许放在workspace。
    if reference.parent.name not in ('config', 'workspace'):
        return None
    return reference.parent.parent / 'workspace/storage/schema_maintenance.json'


def assert_writable(mysql):
    """在每次SQL写入前核对持久维护状态；参数：数据库配置；返回：无。"""
    path = state_path(mysql)
    if not _maintenance_writer.get() and path and path.exists():
        from ecommerce.infrastructure.storage.mysql import MySQLStorageError
        raise MySQLStorageError('数据库维护尚未完成，暂时禁止写入；请重试统一升级并核对结果')


def registered(root):
    """加载正式及人工测试配置并构造验证连接；参数：根目录；返回：三个配置。"""
    production = load_settings(str(root / 'config/settings.toml'), root=root)
    testing = load_settings(str(root / 'config/settings.test.toml'), root=root)
    if production.mysql.database != 'taobao_automation' or testing.mysql.database != 'taobao_automation_test':
        raise ValueError('统一升级要求正式和人工测试配置分别指向登记的库名')
    if production.workspace.resolve() != (root / 'workspace').resolve():
        raise ValueError('正式环境必须使用workspace')
    if (production.mysql.host, production.mysql.port) != (testing.mysql.host, testing.mysql.port):
        raise ValueError('两环境必须指向同一本机MySQL服务')
    validate(testing)
    verification = replace(production, environment='verify', mysql=replace(production.mysql, database='taobao_automation_verify'))
    return [verification, testing, production]


def _save(path, data):
    """原子保存无密码维护报告；参数：路径、内容；返回：无。"""
    from ecommerce.infrastructure.tasks.ownership import atomic_json
    atomic_json(path, data)


def upgrade_all(root):
    """同一批次依次升级验证、人工测试及正式库；参数：根目录；返回：结果报告。"""
    from datetime import datetime
    from ecommerce.infrastructure.storage.mysql import (create_mysql_engine, upgrade_schema,
        inspect_schema, current_revision, MySQLStorageError, REVISION)
    from ecommerce.infrastructure.tasks.lock import all_browser_resources, shop_lock
    configurations = registered(root)
    marker = root / 'workspace/storage/schema_maintenance.json'
    result = {'id': uuid4().hex, 'target': REVISION, 'started_at': datetime.now(BEIJING).isoformat(),
              'status': 'checking', 'databases': {}}
    report = root / 'workspace/output/reports/schema_upgrades' / (result['id'] + '.json')
    with ExitStack() as stack:
        stack.enter_context(shop_lock(root / 'workspace/storage/schema_upgrade_lock'))
        for configuration in configurations[1:]:
            stack.enter_context(all_browser_resources(configuration))
        # 先连通三库/服务并排除任务。只允许人工测试库不存在时显式创建空库。
        for configuration in configurations:
            mysql = configuration.mysql
            engine = create_mysql_engine(replace(mysql, database='mysql'))
            try:
                with engine.connect() as connection:
                    exists = connection.scalar(sa.text('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name'), {'name': mysql.database})
                    if exists:
                        for table, states in (('tasks', ('running',)), ('analysis_tasks', ('running','sending','processing','queued','pending'))):
                            present = connection.scalar(sa.text('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=:db AND TABLE_NAME=:tab'), {'db':mysql.database,'tab':table})
                            if present:
                                statement=sa.text('SELECT COUNT(*) FROM `'+mysql.database+'`.`'+table+'` WHERE status IN :states').bindparams(sa.bindparam('states',expanding=True))
                                if connection.scalar(statement,{'states':states}):
                                    raise MySQLStorageError('仍有运行或待处理任务，未开始维护；请任务结束后重试')
                    if not exists and mysql.database != 'taobao_automation_test':
                        raise MySQLStorageError('正式库或自动验证库不存在，未开始维护')
            finally:
                engine.dispose()
        previous_marker = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else None
        _save(marker, result)
        schema_started = False
        token = _maintenance_writer.set(True)
        try:
            # 标记已阻止其他进程的新SQL写入，再检查已有任务；失败保留标记。
            for configuration in configurations:
                engine = create_mysql_engine(replace(configuration.mysql, database='mysql'))
                try:
                    with engine.begin() as connection:
                        name = configuration.mysql.database
                        exists = connection.scalar(sa.text('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name'), {'name': name})
                        if not exists:
                            connection.exec_driver_sql('CREATE DATABASE `taobao_automation_test` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin')
                finally:
                    engine.dispose()
                engine = create_mysql_engine(configuration.mysql)
                try:
                    with engine.connect() as connection:
                        inspector = sa.inspect(connection)
                        for table in ('tasks', 'analysis_tasks'):
                            if inspector.has_table(table):
                                statuses = ('running', 'sending', 'processing', 'queued', 'pending') if table == 'analysis_tasks' else ('running',)
                                statement = sa.text('SELECT COUNT(*) FROM `' + table + '` WHERE status IN :states').bindparams(sa.bindparam('states', expanding=True))
                                if connection.scalar(statement, {'states': statuses}):
                                    raise MySQLStorageError('仍有运行或待处理任务，请结束任务后重试统一升级')
                        result['databases'][configuration.mysql.database] = {'before': current_revision(connection)}
                finally:
                    engine.dispose()
            structures = {}
            for configuration in configurations:
                name = configuration.mysql.database
                schema_started = True
                result['status'] = 'upgrading'
                _save(marker, result)
                upgrade_schema(configuration.mysql)
                checked = inspect_schema(configuration.mysql)
                result['databases'][name].update(after=checked['revision'], schema_errors=checked['schema_errors'])
                structures[name] = checked['tables']
                if checked['schema_errors']:
                    raise MySQLStorageError('结构检查存在差异，详情见维护报告')
            def canonical(value, key=None):
                """规范结构比较顺序；参数：反射结果；返回：可比较对象。"""
                if key == 'check_constraints':
                    from ecommerce.infrastructure.storage.mysql_checks import check_expression_tree
                    return {name:check_expression_tree(expression) for name,expression in sorted(value.items())}
                if isinstance(value, dict): return {name:canonical(item,name) for name,item in sorted(value.items()) if 'auto_increment' not in name.lower()}
                if isinstance(value, list):
                    items=[canonical(item) for item in value]
                    return sorted(items,key=lambda item:item.get('name','')) if items and all(isinstance(item,dict) for item in items) else items
                return value
            result['comparison'] = '字段按名称比较；历史物理顺序不重排；检查表达式按语法树比较'
            if len({json.dumps(canonical(value),sort_keys=True,default=str) for value in structures.values()}) != 1:
                result['structure_differences'] = '实际结构不同，请分别运行database check核对'
                raise MySQLStorageError('三库实际结构不同')
            result['status'] = 'succeeded'
            result['finished_at'] = datetime.now(BEIJING).isoformat()
            _save(report, result)
            marker.unlink()
        except Exception as error:
            result['status'] = 'failed'
            result['error'] = str(error) if isinstance(error, (MySQLStorageError, ValueError)) else '升级发生异常，请检查实际结构后重试'
            for configuration in configurations:
                try:
                    engine = create_mysql_engine(configuration.mysql)
                    try:
                        with engine.connect() as connection:
                            result['databases'].setdefault(configuration.mysql.database, {})['actual'] = current_revision(connection)
                    finally:
                        engine.dispose()
                except Exception:
                    result['databases'].setdefault(configuration.mysql.database, {})['actual'] = 'unavailable'
            if schema_started:
                _save(marker, result)
            elif previous_marker is not None:
                _save(marker, previous_marker)
            else:
                marker.unlink(missing_ok=True)
            _save(report, result)
            raise MySQLStorageError('统一升级未完成，'+('维护状态已保留' if schema_started or previous_marker is not None else '尚未升级业务表')+'；报告：' + str(report)) from None
        finally:
            _maintenance_writer.reset(token)
    return result
