"""显式启用的本机独立库验证；只插入临时合成记录，结束仅清理本测试归属。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timezone
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from ecommerce.infrastructure.storage.task_store import task_store,fingerprint
from ecommerce.infrastructure.tasks.events import TaskEvents,EventJournal,notify,current_events,replay_events
from ecommerce.infrastructure.tasks.ownership import execution_key,owner_alive
from ecommerce.infrastructure.tasks.runner import run_task
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention,TaskStopped
from ecommerce.shared.logging import setup_logging
from ecommerce.web.app import create_app


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS')=='1','须显式启用独立 MySQL 库验证')
class MySQLTasksTests(unittest.TestCase):
    def setUp(self):
        """建立临时来源与独立库连接；参数：无；返回：无。"""
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base=load_settings(root=Path(temporary.name))
        self.settings=replace(self.base,task_backend='mysql')
        if not self.settings.mysql.database.endswith('_verify'):
            self.fail('测试禁止连接正式库')
        self.store=task_store(self.settings.mysql)
        self.store.initialize()
        self.task_ids=set()
        self.event_keys=set()
        self.addCleanup(self.cleanup_records)

    def cleanup_records(self):
        """按本测试编号清理子记录后清理父记录；参数：无；返回：无。"""
        logger=logging.getLogger('ecommerce')
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        for path in (self.settings.log_dir/'events').glob('*.jsonl'):
            for line in path.read_text(encoding='utf-8').splitlines():
                row=json.loads(line)
                self.event_keys.add(row['event_key'])
                if row.get('task_id'):
                    self.task_ids.add(row['task_id'])
        tables=self.store.tables
        with self.store.engine.begin() as connection:
            events=tables['task_events']
            connection.execute(events.delete().where(sa.or_(events.c.event_key.in_(self.event_keys),events.c.task_id.in_(self.task_ids))))
            connection.execute(tables['tasks'].delete().where(tables['tasks'].c.id.in_(self.task_ids)))

    def new_session(self):
        """创建可追踪合成任务；参数：无；返回：事件会话。"""
        session=TaskEvents(self.settings,self.store,'example.validation')
        self.task_ids.add(session.task_id)
        self.addCleanup(session.close)
        return session

    def event_operation(self,task_id=None,**changes):
        """创建唯一合成事件；参数：任务及覆盖字段；返回：操作。"""
        operation={'op':'event','database':self.settings.mysql.database,'task_id':task_id,'event_key':'test:'+uuid4().hex,
                   'time':datetime.now(timezone.utc).isoformat(),'event_type':'validation','message':'脱敏样例'}
        operation.update(changes)
        self.event_keys.add(operation['event_key'])
        return operation

    def test_task_sequence_concurrent_writes_dedup_and_payload_conflict(self):
        """并发分配连续序号，重复补写不增行，同键异文拒绝覆盖。"""
        session=self.new_session()
        session.start()
        operations=[self.event_operation(session.task_id) for _ in range(12)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.store.apply_operation,operations))
        before=self.store.list_events(task_id=session.task_id)['events']
        self.assertEqual([r['task_sequence'] for r in before],list(range(1,14)))
        self.store.apply_operation(operations[0])
        self.assertEqual(len(self.store.list_events(task_id=session.task_id)['events']),13)
        with self.assertRaises(MySQLStorageError):
            self.store.apply_operation(dict(operations[0],message='冲突'))
        self.assertTrue(session.finish('succeeded','验证实际通过'))
        with self.assertRaises(MySQLStorageError):
            self.store.apply_operation(self.event_operation(session.task_id,op='finish',state='failed'))

    def test_ack_loss_after_real_commit_replays_once(self):
        """真实提交后模拟确认丢失，再补写不得重复或变更序号。"""
        operation=self.event_operation()
        journal=EventJournal(self.settings,uuid4().hex)
        journal.append(operation)
        original=self.store.apply_operation
        def lost_response(value):
            """模拟提交后连接响应丢失；参数：操作；返回：异常。"""
            original(value)
            raise MySQLStorageError('模拟提交回执丢失')
        with patch.object(self.store,'apply_operation',side_effect=lost_response),self.assertRaises(MySQLStorageError):
            journal.drain(self.store)
        self.assertTrue(journal.pending())
        self.assertEqual(journal.drain(self.store),1)
        with self.store.engine.connect() as connection:
            count=connection.scalar(sa.select(sa.func.count()).select_from(self.store.tables['task_events']).where(self.store.tables['task_events'].c.event_key==operation['event_key']))
        self.assertEqual(count,1)

    def test_runner_real_results_and_no_sqlite_write(self):
        """运行入口保存真实结论、命令日志和人工状态；不再写旧任务库。"""
        setup_logging(self.settings)
        def action():
            """执行无浏览器合成流程；参数：无；返回：完成说明。"""
            notify('进度 password=should-hide')
            database.update_task_progress(self.store,current_events().task_id,'needs_attention','等待人工')
            database.update_task_progress(self.store,current_events().task_id,'running','人工完成')
            return '已完成合成验证'
        result=run_task(self.settings,'example.validation',action)
        self.task_ids.add(result.task_id)
        self.assertEqual(result.status,'succeeded')
        events=self.store.list_events(task_id=result.task_id)['events']
        self.assertIn('task_progress',[r['event_type'] for r in events])
        self.assertNotIn('should-hide',json.dumps(events))
        self.assertFalse(self.settings.database.exists())
        for error,status in [(TaskStopped('停止'),'cancelled'),(NeedsAttention('等待'),'needs_attention'),(RuntimeError('合成错误'),'failed')]:
            def failure(error=error):
                """抛出指定合成异常；参数：异常；返回：无。"""
                raise error
            failed=run_task(self.settings,'example.validation',failure)
            self.task_ids.add(failed.task_id)
            self.assertEqual(failed.status,status)
            row=next(r for r in self.store.list_tasks() if r['id']==failed.task_id)
            self.assertEqual(row['status'],status)
            self.assertIsNotNone(row['finished_at'])

    def test_unavailable_finish_reports_pending_and_later_replays_truth(self):
        """业务结束后断库，不伪造入库成功，恢复只补写真结论。"""
        actual=self.store.apply_operation
        def disrupted(operation):
            """仅阻断结束提交；参数：操作；返回：真实提交或异常。"""
            if operation['op']=='finish':
                raise MySQLStorageError('模拟断库')
            return actual(operation)
        with patch.object(self.store,'apply_operation',side_effect=disrupted):
            result=run_task(self.settings,'example.validation',lambda:'真实业务完成')
        self.task_ids.add(result.task_id)
        self.assertEqual(result.status,'needs_attention')
        self.assertIn('待入库',result.message)
        self.assertGreater(replay_events(self.settings,self.store),0)
        row=next(r for r in self.store.list_tasks() if r['id']==result.task_id)
        self.assertEqual((row['status'],row['message']),('succeeded','真实业务完成'))


    def test_web_and_cli_share_filters_escape_html_and_return_503(self):
        """历史接口校验、HTML转义及断库错误明确，不返回假的空记录。"""
        session=self.new_session()
        session.start()
        self.store.apply_operation(self.event_operation(session.task_id,message='<script>bad()</script>',level='ERROR'))
        app=create_app(self.settings)
        client=app.test_client()
        url='/api/events?task_id='+session.task_id+'&level=ERROR'
        response=client.get(url)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json['events']),1)
        page=client.get('/logs?task_id='+session.task_id).data.decode('utf-8')
        self.assertIn('&lt;script&gt;',page)
        self.assertNotIn('<script>bad()',page)
        for query in ['limit=0','after=-1','task_id=invalid','since=2026-10-20T12:00:00','since=2026-10-21T00:00:00Z&until=2026-10-20T00:00:00Z']:
            self.assertEqual(client.get('/api/events?'+query).status_code,400)
        with patch.object(self.store,'list_events',side_effect=MySQLStorageError('模拟连接不可用')):
            self.assertEqual(client.get(url).status_code,503)
        with patch.object(self.store,'list_tasks',side_effect=MySQLStorageError('模拟连接不可用')):
            self.assertEqual(client.get('/tasks').status_code,503)
        self.assertTrue(session.finish('succeeded','接口验证通过'))


    def test_recovery_checks_resource_and_process_identity(self):
        """不恢复仍存活任务，恢复当前失效资源不影响另一资源。"""
        session=self.new_session()
        session.start()
        self.assertEqual(self.store.recover_interrupted(execution_key(self.settings),owner_alive),0)
        other=TaskEvents(replace(self.settings,profile_path=self.base.root/'other_profile'),self.store,'example.other')
        self.addCleanup(other.close)
        self.task_ids.add(other.task_id)
        other.start()
        session.close()
        self.assertEqual(self.store.recover_interrupted(execution_key(self.settings),lambda *_:False),1)
        rows={r['id']:r for r in self.store.list_tasks()}
        self.assertEqual(rows[session.task_id]['status'],'needs_attention')
        self.assertIsNotNone(rows[session.task_id]['finished_at'])
        self.assertEqual(rows[other.task_id]['status'],'running')
        self.assertIsNone(rows[other.task_id]['finished_at'])


if __name__=='__main__':
    unittest.main()
