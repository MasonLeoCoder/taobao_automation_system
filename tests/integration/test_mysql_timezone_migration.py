"""仅在空独立库测试旧时区升级的故障回滚、恢复及重复执行。"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from ecommerce.infrastructure.storage import mysql
from ecommerce.infrastructure.storage.mysql_migrations import schema, schema_v1, schema_v3
from ecommerce.shared.config import load_settings
from tests.mysql_migration_fixture import reset_empty_validation


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_MIGRATION_TESTS') == '1', '显式启用空独立库迁移验证')
class BeijingMigrationTests(unittest.TestCase):
    def test_failure_rolls_back_data_and_version_then_retry_converts_once(self):
        """提交版本前故障必须回滚全部时间；重试及重复升级只能转换一次。"""
        with tempfile.TemporaryDirectory() as folder:
            settings = load_settings(root=Path(folder)).mysql
            self.assertTrue(settings.database.endswith('_verify'), '禁止连接正式库')
            engine = mysql.create_mysql_engine(settings, migration=True)
            self.addCleanup(engine.dispose)
            metadata = schema_v1.build_metadata()
            tasks, events = metadata.tables['tasks'], metadata.tables['task_events']
            task_id = uuid4().hex
            with engine.connect() as connection:
                reset_empty_validation(connection,metadata,schema.PREVIOUS_REVISION)
                self.assertIn(mysql.current_revision(connection), (schema.PREVIOUS_REVISION, schema.REVISION, schema_v3.REVISION, mysql.REVISION))
                counts = {name: connection.scalar(sa.select(sa.func.count()).select_from(table))
                          for name, table in metadata.tables.items()}
                self.assertTrue(all(n == 0 for n in counts.values()), '验证库非空，不重置时区或写入样例')
                connection.commit()
                # 只允许空验证库回到旧结构口径，以便重复运行这一升级回归。
                operations = Operations(MigrationContext.configure(connection))
                for table in metadata.tables.values():
                    for column in table.c:
                        if isinstance(column.type, sa.DateTime):
                            operations.alter_column(table.name, column.name, existing_type=column.type,
                                                    existing_nullable=column.nullable, comment=column.comment)
                connection.execute(sa.text('UPDATE alembic_version SET version_num=:old'), {'old': schema.PREVIOUS_REVISION})
                connection.commit()
            original = datetime(2026, 10, 23, 23, 30, 0, 123456)
            with engine.begin() as connection:
                connection.execute(tasks.insert().values(id=task_id, name='timezone.migration.validation',
                    status='succeeded', started_at=original, finished_at=original+timedelta(hours=1),
                    owner_process_started_at=original-timedelta(seconds=1), origin='live', last_event_sequence=2))
                for sequence in (1, 2):
                    connection.execute(events.insert().values(task_id=task_id, task_sequence=sequence,
                        event_type='validation', occurred_at=original if sequence == 1 else None,
                        recorded_at=original, time_raw='2026-10-23T23:30:00.123456+00:00',
                        time_basis='explicit_offset' if sequence == 1 else 'unknown',
                        origin='live', event_key=uuid4().hex, details={'source': 'synthetic'}))
            injected = []

            def fail_before_version_commit(connection, cursor, statement, parameters, context, executemany):
                """只在新版本提交前注入一次合成故障，验证时间更新没有提前提交。"""
                if (not injected and statement.lstrip().upper().startswith('UPDATE ALEMBIC_VERSION')
                        and (schema.REVISION in statement or schema.REVISION in str(parameters))):
                    injected.append(True)
                    raise RuntimeError('合成版本提交前故障')

            sa.event.listen(engine, 'before_cursor_execute', fail_before_version_commit)
            try:
                with patch.object(mysql, 'create_mysql_engine', return_value=engine):
                    with self.assertRaisesRegex(RuntimeError, '合成版本提交前故障'):
                        mysql.upgrade_schema(settings)
                self.assertTrue(injected)
                with engine.connect() as connection:
                    self.assertEqual(mysql.current_revision(connection), schema.PREVIOUS_REVISION)
                    self.assertEqual(connection.scalar(sa.select(tasks.c.started_at).where(tasks.c.id == task_id)), original)
                    self.assertEqual(connection.scalar(sa.select(events.c.recorded_at).where(events.c.task_id == task_id).limit(1)), original)
                sa.event.remove(engine, 'before_cursor_execute', fail_before_version_commit)
                self.assertEqual(mysql.upgrade_schema(settings), mysql.REVISION)
                tasks,events=mysql.build_metadata().tables['tasks'],mysql.build_metadata().tables['task_events']
                with engine.connect() as connection:
                    row = connection.execute(sa.select(tasks).where(tasks.c.id == task_id)).mappings().one()
                    self.assertEqual(row['started_at'], original+timedelta(hours=8))
                    self.assertEqual(row['finished_at'], original+timedelta(hours=9))
                    self.assertEqual(row['owner_process_started_at'], original+timedelta(hours=8, seconds=-1))
                    rows = connection.execute(sa.select(events).where(events.c.task_id == task_id).order_by(events.c.task_sequence)).mappings().all()
                    self.assertEqual(rows[0]['occurred_at'], original+timedelta(hours=8))
                    self.assertIsNone(rows[1]['occurred_at'])
                    self.assertTrue(all(r['recorded_at'] == original+timedelta(hours=8) for r in rows))
                    self.assertTrue(all(r['time_raw'] == '2026-10-23T23:30:00.123456+00:00' and r['details'] == {'source': 'synthetic'} for r in rows))
                self.assertEqual(mysql.upgrade_schema(settings), mysql.REVISION)
                with engine.connect() as connection:
                    self.assertEqual(connection.scalar(sa.select(tasks.c.started_at).where(tasks.c.id == task_id)), original+timedelta(hours=8))
                actual = mysql.inspect_schema(settings)
                self.assertEqual(actual['schema_errors'], [])
                if os.environ.get('ECOMMERCE_MYSQL_TIMEZONE_REPORT'):
                    report = {'database': settings.database, 'revision': schema.REVISION, 'passed': True,
                              'rollback_data_and_version': True, 'retry_converts_once': True,
                              'repeat_upgrade_does_not_shift_again': True, 'null_and_microseconds_preserved': True,
                              'source_time_and_json_unchanged': True, 'comments_and_relationships_checked': True}
                    Path(os.environ['ECOMMERCE_MYSQL_TIMEZONE_REPORT']).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            finally:
                if sa.event.contains(engine, 'before_cursor_execute', fail_before_version_commit):
                    sa.event.remove(engine, 'before_cursor_execute', fail_before_version_commit)
                with engine.begin() as connection:
                    connection.execute(events.delete().where(events.c.task_id == task_id))
                    connection.execute(tasks.delete().where(tasks.c.id == task_id))
                mysql.upgrade_schema(settings)
