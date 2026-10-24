"""独立库验证北京时间实际写入、旧流水补写、时区筛选和网页显示。"""
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import sqlalchemy as sa

from ecommerce.infrastructure.storage.task_store import task_store
from ecommerce.infrastructure.storage.time_values import BEIJING, database_now
from ecommerce.infrastructure.tasks.events import EventJournal
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS') == '1', '显式启用独立MySQL验证')
class MySQLTimezoneTests(unittest.TestCase):
    def test_utc_journal_replay_is_beijing_once_and_filters_show_same_instant(self):
        """旧UTC流水补写为北京时间，重复补写不偏移，网页和查询按同一时刻筛选。"""
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(load_settings(root=Path(folder)), task_backend='mysql')
            self.assertTrue(settings.mysql.database.endswith('_verify'), '禁止连接正式库')
            store = task_store(settings.mysql)
            store.initialize()
            task_id = uuid4().hex
            journal = EventJournal(settings, task_id)
            operations = [
                {'op': 'create', 'database': settings.mysql.database, 'task_id': task_id,
                 'event_key': uuid4().hex, 'time': '2026-10-23T23:59:59.123456+00:00',
                 'event_type': 'task_started', 'state': 'running', 'message': '旧UTC来源',
                 'task': {'id': task_id, 'name': 'timezone.validation', 'status': 'running',
                          'started_at': '2026-10-23T23:59:59.123456+00:00', 'origin': 'live'}},
                {'op': 'finish', 'database': settings.mysql.database, 'task_id': task_id,
                 'event_key': uuid4().hex, 'time': '2026-10-24T00:00:00.654321+00:00',
                 'event_type': 'task_finished', 'state': 'succeeded', 'message': '北京时间验证完成'},
            ]
            try:
                for operation in operations:
                    journal.append(operation)
                self.assertEqual(journal.drain(store), 2)
                for operation in operations:
                    store.apply_operation(operation)
                with store.engine.connect() as connection:
                    task = connection.execute(sa.select(store.tables['tasks']).where(store.tables['tasks'].c.id == task_id)).mappings().one()
                    self.assertEqual(task['started_at'], datetime(2026, 10, 24, 7, 59, 59, 123456))
                    self.assertEqual(task['finished_at'], datetime(2026, 10, 24, 8, 0, 0, 654321))
                    self.assertEqual(connection.scalar(sa.text('SELECT @@SESSION.time_zone')), '+08:00')
                utc = store.list_events(task_id=task_id, since='2026-10-24T00:00:00+00:00')['events']
                local = store.list_events(task_id=task_id, since='2026-10-24T08:00:00+08:00')['events']
                self.assertEqual(utc, local)
                self.assertEqual(len(local), 1)
                self.assertEqual(local[0]['occurred_at'], '2026-10-24T08:00:00.654321+08:00')
                self.assertEqual(local[0]['time_raw'], operations[1]['time'])
                recorded = datetime.fromisoformat(local[0]['recorded_at'])
                self.assertLess(abs((datetime.now(BEIJING)-recorded).total_seconds()), 10)
                client = create_app(settings).test_client()
                self.assertIn('10-24 07:59:59', client.get('/tasks').data.decode('utf-8'))
                api = client.get('/api/events?task_id='+task_id).json
                self.assertTrue(all(e['occurred_at'].endswith('+08:00') for e in api['events']))
                self.assertFalse(journal.pending())
            finally:
                with store.engine.begin() as connection:
                    connection.execute(store.tables['task_events'].delete().where(store.tables['task_events'].c.task_id == task_id))
                    connection.execute(store.tables['tasks'].delete().where(store.tables['tasks'].c.id == task_id))
