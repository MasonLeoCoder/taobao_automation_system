"""数据库迁移、核对与实时事件补写；命令不触发浏览器业务。"""
from datetime import datetime
import json
import logging
from pathlib import Path
from uuid import uuid4

from ecommerce.infrastructure.storage.mysql import MySQLStorageError,inspect_schema,upgrade_schema
from ecommerce.infrastructure.storage.mysql_dictionary import render_dictionary
from ecommerce.infrastructure.storage.mysql_verification import verify_foundation


def execute(settings,args):
    """执行基础命令；参数：应用配置、命令参数；返回：退出码。"""
    def resolve(value):
        """解析输出路径；参数：文本；返回：绝对路径。"""
        path=Path(value)
        return (settings.root/path).resolve() if not path.is_absolute() else path.resolve()
    try:
        if args.action=='replay-events':
            from ecommerce.infrastructure.tasks.collection_control import replay_emergency
            from ecommerce.infrastructure.tasks.events import replay_events
            from ecommerce.infrastructure.storage.task_store import task_store
            store=task_store(settings.mysql)
            store.initialize()
            replay_emergency(settings)
            print('本次确认补写事件：'+str(replay_events(settings,store))+'；未重放任何业务动作')
            return 0
        if args.action=='migrate-collection-jobs':
            from ecommerce.infrastructure.tasks.lock import all_browser_resources
            from ecommerce.infrastructure.storage.collection_job_migration import migrate_dispatch_files
            from ecommerce.infrastructure.storage.task_store import task_store
            task_store(settings.mysql).initialize()
            with all_browser_resources(settings):
                report=migrate_dispatch_files(settings)
            path=settings.workspace/'output/reports'/('collection_jobs_migration_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8]+'.json')
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('x',encoding='utf-8') as handle:
                json.dump(report,handle,ensure_ascii=False,indent=2)
            print('派发申请迁移完成，未启动采集；报告：'+str(path))
            return 0
        if args.action=='upgrade':
            from ecommerce.infrastructure.tasks.lock import all_browser_resources
            with all_browser_resources(settings):
                print('MySQL 结构升级完成；当前版本：'+upgrade_schema(settings.mysql))
            return 0
        if args.action=='dictionary':
            path=resolve(args.output)
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(render_dictionary(),encoding='utf-8')
            print('完整字段字典：'+str(path))
            return 0
        if args.action=='verify':
            report=verify_foundation(settings.mysql)
            default=settings.workspace/'output/reports'/('mysql_foundation_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8]+'.json')
            path=resolve(args.report) if args.report else default
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('x',encoding='utf-8') as handle:
                json.dump(report,handle,ensure_ascii=False,indent=2)
                handle.write('\n')
            print(('MySQL 基础验证通过' if report['passed'] else 'MySQL 基础验证未全部通过')+'；报告：'+str(path))
            return 0 if report['passed'] else 1
        report=inspect_schema(settings.mysql)
        print(json.dumps({k:v for k,v in report.items() if k not in ('tables','grants')},ensure_ascii=False,indent=2))
        return 0 if not report['schema_errors'] else 1
    except MySQLStorageError as error:
        logging.getLogger(__name__).error('数据库命令失败：%s',error)
        raise
