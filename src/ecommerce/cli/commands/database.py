"""数据库迁移、核对、历史导入与事件补写；命令不触发浏览器业务。"""
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
        if args.action=='import-collection-history':
            from ecommerce.modules.collection.history import snapshot_history,preview_history,import_history,audit_history,history_lock
            if args.audit and not args.snapshot:
                raise ValueError('只读复核须提供 --snapshot 原固定清单')
            with history_lock(settings):
                manifest=resolve(args.snapshot) if args.snapshot else snapshot_history(settings)
                if args.audit:
                    report=audit_history(settings,manifest)
                elif args.apply:
                    report=import_history(settings,manifest)
                    report['audit']=audit_history(settings,manifest)
                else:
                    report=preview_history(settings,manifest)
            path=resolve(args.report) if args.report else settings.workspace/'output/reports'/('collection_history_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8]+'.json')
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('x',encoding='utf-8') as handle:
                json.dump(report,handle,ensure_ascii=False,indent=2)
            print(json.dumps(report,ensure_ascii=False,indent=2))
            print('报告：'+str(path))
            return 0
        if args.action=='replay-events':
            from ecommerce.infrastructure.tasks.events import replay_events
            from ecommerce.infrastructure.storage.task_store import task_store
            store=task_store(settings.mysql)
            store.initialize()
            print('本次确认补写事件：'+str(replay_events(settings,store))+'；未重放任何业务动作')
            return 0
        if args.action=='import-task-history':
            from ecommerce.infrastructure.storage.task_history_import import snapshot_history,preview_history,import_history,audit_history
            from ecommerce.infrastructure.storage.task_store import task_store
            from ecommerce.infrastructure.tasks.lock import shop_lock
            with shop_lock(settings.profile_path):
                manifest=resolve(args.snapshot) if args.snapshot else snapshot_history(settings,take_lock=False)
                report=import_history(task_store(settings.mysql),manifest) if args.apply else preview_history(manifest)
                if args.apply:
                    report['audit']=audit_history(task_store(settings.mysql),manifest)
            default=settings.workspace/'output/reports'/('task_history_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:8]+'.json')
            path=resolve(args.report) if args.report else default
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('x',encoding='utf-8') as handle:
                json.dump(report,handle,ensure_ascii=False,indent=2)
            print(json.dumps(report,ensure_ascii=False,indent=2))
            print('报告：'+str(path))
            return 0
        if args.action=='upgrade':
            from ecommerce.modules.collection.history import history_lock
            with history_lock(settings):
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
