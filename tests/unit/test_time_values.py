"""北京时间的存取、跨日、时区缺失及原模型兼容性检查。"""
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import unittest

import sqlalchemy as sa

from ecommerce.infrastructure.storage.time_values import BEIJING, database_time, database_now, iso_value
from ecommerce.infrastructure.storage.mysql_migrations import schema, schema_v1
from ecommerce.infrastructure.storage.collection_store import database_values
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app
from ecommerce.shared.logging import BeijingFormatter
import logging


class BeijingTimeTests(unittest.TestCase):
    def test_terminal_and_file_log_format_uses_beijing(self):
        """日志显示不依赖操作系统时区，原文本格式仍兼容历史解析器。"""
        record = logging.LogRecord('sample', logging.INFO, '', 1, '脱敏样例', (), None)
        record.created = datetime(2026, 10, 23, 23, 30, tzinfo=timezone.utc).timestamp()
        record.msecs = 123
        self.assertEqual(BeijingFormatter().formatTime(record), '2026-10-24 07:30:00,123')

    def test_equivalent_offsets_cross_midnight_and_preserve_microseconds(self):
        """同一时刻的不同偏移输入得到相同北京时间，保留跨日和微秒。"""
        expected = datetime(2026, 10, 24, 7, 59, 59, 123456)
        self.assertEqual(database_time('2026-10-23T23:59:59.123456Z'), expected)
        self.assertEqual(database_time('2026-10-24T07:59:59.123456+08:00'), expected)
        self.assertEqual(database_time(datetime(2026, 10, 23, 23, 59, 59, 123456, timezone.utc)), expected)
        self.assertEqual(iso_value(expected), '2026-10-24T07:59:59.123456+08:00')
        self.assertEqual(iso_value(datetime(2026, 10, 23, 23, 59, 59, 123456, timezone.utc)),
                         '2026-10-24T07:59:59.123456+08:00')

    def test_missing_time_is_preserved_but_unzoned_source_is_rejected(self):
        """空值不补造时间，无时区外部来源不能按主机时区猜测。"""
        self.assertIsNone(database_time(None))
        self.assertIsNone(iso_value(None))
        for value in ('2026-10-23T12:00:00', datetime(2026, 10, 23, 12)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                database_time(value)
        self.assertLess(abs((database_now()-datetime.now(BEIJING).replace(tzinfo=None)).total_seconds()), 1)

    def test_current_metadata_keeps_frozen_schema_and_all_relationships(self):
        """不改写首版迁移，当前模型只更新全部时间备注而保持字段和关系。"""
        old, new = schema_v1.build_metadata(), schema.build_metadata()
        self.assertEqual(set(old.tables), set(new.tables))
        for name, table in new.tables.items():
            self.assertEqual(set(table.c.keys()), set(old.tables[name].c.keys()))
            self.assertEqual(len(table.foreign_key_constraints), len(old.tables[name].foreign_key_constraints))
            for column in table.c:
                if isinstance(column.type, sa.DateTime):
                    self.assertIn('北京时间（UTC+08:00）', column.comment)
                    self.assertIn('UTC 时间', old.tables[name].c[column.name].comment)
                else:
                    self.assertEqual(column.comment, old.tables[name].c[column.name].comment)

    def test_collection_conversion_and_web_display_do_not_apply_offset_twice(self):
        """采集写入及网页显示同一北京时间，不发生重复偏移。"""
        table = schema.build_metadata().tables['collection_batches']
        value = database_values(table, {'captured_at': '2026-10-23T16:30:00.123456Z'})
        self.assertEqual(value['captured_at'], datetime(2026, 10, 24, 0, 30, 0, 123456))
        self.assertEqual(database_values(table, value), value)
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(load_settings(root=Path(folder)))
            self.assertEqual(app.jinja_env.filters['local_time'](iso_value(value['captured_at'])), '10-24 00:30:00')
