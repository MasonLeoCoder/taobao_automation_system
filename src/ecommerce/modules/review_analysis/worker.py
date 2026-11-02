"""独立评价分析工作进程：复用通用任务日志，不获取浏览器资源锁。"""
import argparse
import os
from ecommerce.shared.config import load_settings, prepare_workspace
from ecommerce.shared.logging import setup_logging
from ecommerce.infrastructure.tasks.events import TaskEvents, event_session
from ecommerce.infrastructure.tasks.lock import shop_lock
from ecommerce.infrastructure.tasks.ownership import process_started_at
from ecommerce.infrastructure.tasks.ownership import owner_alive
from ecommerce.infrastructure.storage import analysis_store as repository
from ecommerce.infrastructure.storage.task_store import fingerprint
from ecommerce.modules.review_analysis import service


def run(settings, task_id):
    """在任务专属锁内领取和执行；参数：配置及分析编号；返回：退出码。"""
    service.identifier(task_id)
    with shop_lock(settings.workspace / 'storage/analysis_workers' / task_id):
        data = repository.read(settings, task_id)
        if data['task']['status'] != 'queued':
            return 0
        kind=data['task']['analysis_type']
        label='综合选品分析' if kind=='composite' else '问答分析' if kind=='questions' else '评价分析'
        events = TaskEvents(settings, settings.task_database, 'analysis.'+kind)
        # 浏览器任务恢复不应接管独立AI进程。
        events.owner['execution_key'] = fingerprint({'analysis_id': task_id, 'root': str(settings.root), 'database': settings.mysql.database})
        try:
            events.store.initialize()
            if data['task'].get('execution_task_id'):
                from ecommerce.infrastructure.tasks.events import EventJournal
                EventJournal(settings, data['task']['execution_task_id']).drain(events.store)
            events.store.recover_interrupted(events.owner['execution_key'], owner_alive)
            events.start()
            repository.update_task(settings, task_id, status='running', execution_task_id=events.task_id,
                owner={'pid': os.getpid(), 'started_at': process_started_at(os.getpid())}, message='后台已启动。')
            with event_session(events):
                status = service.execute(settings, task_id, events)
            events.finish('succeeded' if status == 'succeeded' else 'failed', label+'：' + status + '；分析编号：' + task_id)
            return 0 if status == 'succeeded' else 1
        except Exception:
            # 不输出上游异常或SQL参数；收据保留，人工恢复不自动重发。
            try:
                repository.update_task(settings, task_id, status='interrupted', active_key=None, owner=None,
                    message='分析进程中断，已保存资料和响应收据保留；请查看调用记录并手动继续。')
                events.finish('failed', label+'中断；分析编号：' + task_id)
            except Exception:
                pass
            return 1
        finally:
            events.close()


def main():
    """解析内部进程参数；参数：命令行；返回：退出码。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--task-id', required=True)
    args = parser.parse_args()
    from pathlib import Path
    settings = load_settings(args.config, root=Path(args.root))
    prepare_workspace(settings)
    setup_logging(settings)
    return run(settings, args.task_id)


if __name__ == '__main__':
    raise SystemExit(main())
