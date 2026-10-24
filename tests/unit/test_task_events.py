"""不连接真实数据库验证日志落盘、补写、暂停控制和历史解析边界。"""
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from ecommerce.infrastructure.storage.task_history_import import snapshot_history,preview_history,read_snapshot
from ecommerce.infrastructure.tasks.events import EventJournal,TaskEvents,sanitize,notify,current_events,event_session
from ecommerce.infrastructure.tasks.ownership import owner_alive,process_started_at,atomic_json
from ecommerce.modules.account.taobao_control import request_control
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import TaskStopped,NeedsAttention
from ecommerce.shared.logging import setup_logging


class MemoryStore:
    """模拟已提交但确认响应丢失的数据库；不会被真实程序使用。"""
    def __init__(self,settings):
        self.settings=settings
        self.rows={}
        self.lose_response=False

    def apply_operation(self,operation):
        self.rows.setdefault(operation['event_key'],operation)
        if self.lose_response:
            self.lose_response=False
            raise MySQLStorageError('测试提交响应丢失')


class TaskEventsTests(unittest.TestCase):
    def test_browser_greenlet_inherits_thread_task_without_cross_thread_leak(self):
        """同步浏览器回调保留任务，嵌套作用域恢复，其他线程及退出后均无关联。"""
        from greenlet import greenlet
        import threading
        outer,inner=object(),object()
        observed=[]
        self.assertIsNone(current_events())
        with event_session(outer):
            self.assertIs(greenlet(current_events).switch(),outer)
            with event_session(inner):
                self.assertIs(greenlet(current_events).switch(),inner)
            self.assertIs(current_events(),outer)
            worker=threading.Thread(target=lambda:observed.append(current_events()))
            worker.start()
            worker.join()
        self.assertEqual(observed,[None])
        self.assertIsNone(greenlet(current_events).switch())

    def setUp(self):
        """使用临时目录与默认无凭据配置；参数：无；返回：无。"""
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings=load_settings(root=Path(temporary.name))
        self.store=MemoryStore(self.settings.mysql)

    def test_unknown_commit_is_replayed_once(self):
        """确认丢失时保留流水，重试只产生一条记录。"""
        journal=EventJournal(self.settings,uuid4().hex)
        operation={'database':self.settings.mysql.database,'event_key':uuid4().hex,'message':'本地先保存'}
        journal.append(operation)
        self.store.lose_response=True
        with self.assertRaises(MySQLStorageError):
            journal.drain(self.store)
        self.assertTrue(journal.pending())
        self.assertEqual(journal.drain(self.store),1)
        self.assertFalse(journal.pending())
        self.assertEqual(len(self.store.rows),1)
        self.assertEqual(journal.drain(self.store),0)

    def test_journal_cannot_cross_databases_or_ignore_partial_tail(self):
        """跨库和不完整记录均停止补写，不推进确认位置。"""
        journal=EventJournal(self.settings,uuid4().hex)
        journal.append({'database':'another_db','event_key':uuid4().hex})
        with self.assertRaises(MySQLStorageError):
            journal.drain(self.store)
        self.assertFalse(journal.ack.exists())
        journal.path.write_bytes(b'{"unfinished":')
        with self.assertRaises(NeedsAttention):
            journal.drain(self.store)

    def test_stop_works_without_database_and_replies_cannot_bypass_wait(self):
        """断库时依赖本地进程归属发送停止；继续不能绕过存储暂停。"""
        settings=replace(self.settings,task_backend='mysql')
        session=TaskEvents(settings,self.store,'collection.taobao.detail.open')
        self.addCleanup(session.close)
        session.error=MySQLStorageError('测试断库')
        atomic_json(session.active_path,dict(session.owner,storage_pending=True))
        with self.assertRaises(ValueError):
            request_control(settings,'continue')
        self.assertIn('已发送停止',request_control(settings,'stop'))
        with patch('builtins.print'),self.assertRaises(TaskStopped):
            session.checkpoint('下一步')
        self.assertTrue(session.journal.pending())

    def test_ack_sharing_conflict_is_retried_without_resetting_offset(self):
        """确认文件短暂占用后读取原位置，不回退为0或丢弃确认文件。"""
        journal=EventJournal(self.settings,uuid4().hex)
        atomic_json(journal.ack,{'offset':123})
        with patch.object(Path,'read_text',side_effect=[PermissionError('占用'),PermissionError('占用'),'{"offset":123}']) as read, \
                patch('ecommerce.infrastructure.tasks.events.time.sleep') as sleep:
            self.assertEqual(journal._offset(),123)
        self.assertEqual(read.call_count,3)
        self.assertEqual(sleep.call_count,2)
        self.assertEqual(json.loads(journal.ack.read_text(encoding='utf-8')),{'offset':123})

    def test_ack_permanent_failure_and_invalid_content_still_block(self):
        """持续占用最多3次，损坏内容直接阻断；两种情况都不改确认文件。"""
        journal=EventJournal(self.settings,uuid4().hex)
        atomic_json(journal.ack,{'offset':123})
        with patch.object(Path,'read_text',side_effect=PermissionError('持续占用')) as read, \
                patch('ecommerce.infrastructure.tasks.events.time.sleep') as sleep:
            with self.assertRaisesRegex(NeedsAttention,'确认文件无法核对'):
                journal._offset()
        self.assertEqual(read.call_count,3)
        self.assertEqual(sleep.call_count,2)
        for raw in ('{broken','{"offset":-1}','{"offset":true}','{}'):
            with self.subTest(raw=raw), patch.object(Path,'read_text',return_value=raw) as read, \
                    patch('ecommerce.infrastructure.tasks.events.time.sleep') as sleep:
                with self.assertRaises(NeedsAttention):
                    journal._offset()
                read.assert_called_once()
                sleep.assert_not_called()
        self.assertEqual(json.loads(journal.ack.read_text(encoding='utf-8')),{'offset':123})

    def test_atomic_ack_replacement_retries_and_preserves_old_file_on_failure(self):
        """替换占用时复用同一临时文件；重试耗尽保留旧确认且清理临时文件。"""
        destination=self.settings.workspace/'ack-test.json'
        atomic_json(destination,{'offset':10})
        original=Path.replace
        calls=[]
        def replace_after_contention(source,target):
            calls.append(source)
            if len(calls)<3:
                raise PermissionError('文件被读取占用')
            return original(source,target)
        with patch.object(Path,'replace',autospec=True,side_effect=replace_after_contention), \
                patch('ecommerce.infrastructure.tasks.ownership.time.sleep'):
            atomic_json(destination,{'offset':20})
        self.assertEqual(len(calls),3)
        self.assertEqual(len(set(calls)),1)
        self.assertEqual(json.loads(destination.read_text(encoding='utf-8')),{'offset':20})
        with patch.object(Path,'replace',side_effect=PermissionError('持续占用')) as replace, \
                patch('ecommerce.infrastructure.tasks.ownership.time.sleep'):
            with self.assertRaises(PermissionError):
                atomic_json(destination,{'offset':30})
        self.assertEqual(replace.call_count,3)
        self.assertEqual(json.loads(destination.read_text(encoding='utf-8')),{'offset':20})
        self.assertFalse(list(destination.parent.glob('.ack-test.json-*.tmp')))

    def test_ack_sharing_conflict_is_retried_without_resetting_offset(self):
        """确认文件短暂占用后读取原位置，不回退为0或丢弃确认文件。"""
        journal=EventJournal(self.settings,uuid4().hex)
        atomic_json(journal.ack,{'offset':123})
        with patch.object(Path,'read_text',side_effect=[PermissionError('占用'),PermissionError('占用'),'{"offset":123}']) as read, \
                patch('ecommerce.infrastructure.tasks.events.time.sleep') as sleep:
            self.assertEqual(journal._offset(),123)
        self.assertEqual(read.call_count,3)
        self.assertEqual(sleep.call_count,2)
        self.assertEqual(json.loads(journal.ack.read_text(encoding='utf-8')),{'offset':123})

    def test_ack_permanent_failure_and_invalid_content_still_block(self):
        """持续占用最多3次，损坏内容直接阻断；两种情况都不改确认文件。"""
        journal=EventJournal(self.settings,uuid4().hex)
        atomic_json(journal.ack,{'offset':123})
        with patch.object(Path,'read_text',side_effect=PermissionError('持续占用')) as read, \
                patch('ecommerce.infrastructure.tasks.events.time.sleep') as sleep:
            with self.assertRaisesRegex(NeedsAttention,'确认文件无法核对'):
                journal._offset()
        self.assertEqual(read.call_count,3)
        self.assertEqual(sleep.call_count,2)
        for raw in ('{broken','{"offset":-1}','{"offset":true}','{}'):
            with self.subTest(raw=raw), patch.object(Path,'read_text',return_value=raw) as read, \
                    patch('ecommerce.infrastructure.tasks.events.time.sleep') as sleep:
                with self.assertRaises(NeedsAttention):
                    journal._offset()
                read.assert_called_once()
                sleep.assert_not_called()
        self.assertEqual(json.loads(journal.ack.read_text(encoding='utf-8')),{'offset':123})

    def test_atomic_ack_replacement_retries_and_preserves_old_file_on_failure(self):
        """替换占用时复用同一临时文件；重试耗尽保留旧确认且清理临时文件。"""
        destination=self.settings.workspace/'ack-test.json'
        atomic_json(destination,{'offset':10})
        original=Path.replace
        calls=[]
        def replace_after_contention(source,target):
            calls.append(source)
            if len(calls)<3:
                raise PermissionError('文件被读取占用')
            return original(source,target)
        with patch.object(Path,'replace',autospec=True,side_effect=replace_after_contention), \
                patch('ecommerce.infrastructure.tasks.ownership.time.sleep'):
            atomic_json(destination,{'offset':20})
        self.assertEqual(len(calls),3)
        self.assertEqual(len(set(calls)),1)
        self.assertEqual(json.loads(destination.read_text(encoding='utf-8')),{'offset':20})
        with patch.object(Path,'replace',side_effect=PermissionError('持续占用')) as replace, \
                patch('ecommerce.infrastructure.tasks.ownership.time.sleep'):
            with self.assertRaises(PermissionError):
                atomic_json(destination,{'offset':30})
        self.assertEqual(replace.call_count,3)
        self.assertEqual(json.loads(destination.read_text(encoding='utf-8')),{'offset':20})
        self.assertFalse(list(destination.parent.glob('.ack-test.json-*.tmp')))

    def test_pid_reuse_and_unknown_owner_are_distinct(self):
        """进程编号相同而创建时间不同不能被认作原任务。"""
        actual=process_started_at(os.getpid())
        self.assertIsNotNone(actual)
        self.assertIs(owner_alive(os.getpid(),actual),True)
        self.assertIs(owner_alive(os.getpid(),'2000-01-01T00:00:00+00:00'),False)
        self.assertIsNone(owner_alive(None,None))

    def test_sensitive_fields_urls_and_cookie_lines_are_removed(self):
        """日志脱敏覆盖结构、URL签名以及整行 Cookie/Authorization。"""
        value=sanitize({'password':'unique-secret','url':'https://user:pass@example.com/item?id=123&token=private',
                        'message':'Cookie: sid=abc; other=def\nAuthorization: Bearer abc\npassword=xyz'})
        text=json.dumps(value)
        for secret in ['unique-secret','private','user:pass','sid=abc','other=def','Bearer abc','xyz']:
            self.assertNotIn(secret,text)
        self.assertIn('id=123',text)

    def test_notify_keeps_terminal_and_file_once(self):
        """普通进度打印一次，同时写入运行文件并脱敏。"""
        setup_logging(self.settings)
        self.addCleanup(self._close_logging)
        with patch('builtins.print') as terminal:
            notify('进度 password=test-token')
        self.assertEqual(terminal.call_count,1)
        saved=(self.settings.log_dir/'run.log').read_text(encoding='utf-8')
        self.assertEqual(saved.count('进度'),1)
        self.assertNotIn('test-token',saved)

    @staticmethod
    def _close_logging():
        """关闭临时文件句柄；参数：无；返回：无。"""
        logger=logging.getLogger('ecommerce')
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    def test_snapshot_preserves_original_and_unknown_text_time(self):
        """固定来源、组合异常续行，不推断旧文本的时区或任务归属。"""
        database.initialize(self.settings.database)
        key=database.create_task(self.settings.database,'example.history')
        database.finish_task(self.settings.database,key,'failed','原结果')
        path=self.settings.log_dir/'run.log'
        path.parent.mkdir(parents=True)
        content='2026-10-20 12:01:00,000 [ERROR] ecommerce.example - 失败\nTraceback\n  more\n'
        path.write_text(content,encoding='utf-8')
        manifest=snapshot_history(self.settings)
        report=preview_history(manifest)
        self.assertEqual(report['records'],{'tasks':1,'task_events':1})
        self.assertEqual(report['unknown_event_time'],1)
        self.assertEqual(path.read_text(encoding='utf-8'),content)
        _,parsed=read_snapshot(manifest)
        event=next(values for _,records in parsed for table,_,values,_ in records if table=='task_events')
        self.assertIsNone(event['occurred_at'])
        self.assertIsNone(event['task_id'])
        self.assertIn('Traceback\n  more',event['message'])
        path.write_text(content+'late text',encoding='utf-8')
        self.assertEqual(preview_history(manifest)['records'],report['records'])
        (manifest.parent/'run.log').write_text('tampered',encoding='utf-8')
        with self.assertRaises(MySQLStorageError):
            preview_history(manifest)

    def test_active_legacy_task_blocks_import(self):
        """旧来源未结束时不擅自迁移为完成或重跑。"""
        database.initialize(self.settings.database)
        database.create_task(self.settings.database,'example.running')
        with self.assertRaises(MySQLStorageError):
            preview_history(snapshot_history(self.settings))

    def test_data_conflict_stops_instead_of_waiting_for_database_recovery(self):
        """永久内容冲突不当作连接抖动反复等待，下一检查点停止动作。"""
        from ecommerce.infrastructure.storage.collection_store import CollectionDataError
        session=TaskEvents(self.settings,self.store,'example.conflict')
        self.addCleanup(session.close)
        session.error=CollectionDataError('采集归属冲突')
        with self.assertRaisesRegex(NeedsAttention,'归属冲突'):
            session.checkpoint('下一步')


if __name__=='__main__':
    unittest.main()
