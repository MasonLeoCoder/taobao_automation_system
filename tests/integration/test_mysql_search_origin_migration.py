"""只在空验证库演练来源迁移的冲突、中断恢复及重复升级。"""
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
from ecommerce.infrastructure.storage.mysql_migrations import schema, schema_v3
from ecommerce.shared.config import load_settings
from tests.mysql_migration_fixture import reset_empty_validation


@unittest.skipUnless(os.environ.get('ECOMMERCE_MYSQL_MIGRATION_TESTS')=='1','显式启用空验证库来源迁移测试')
class SearchOriginMigrationTests(unittest.TestCase):
    def test_conflict_partial_ddl_and_version_failure_recover_without_changing_original(self):
        """证据冲突先拒绝，DDL中断能重入，数据与版本同时回滚，成功后重复升级不变。"""
        with tempfile.TemporaryDirectory() as folder:
            settings=load_settings(root=Path(folder)).mysql
            self.assertTrue(settings.database.endswith('_verify'),'禁止连接正式库')
            engine=mysql.create_mysql_engine(settings,migration=True)
            self.addCleanup(engine.dispose)
            old,new=schema.build_metadata(),schema_v3.build_metadata()
            with engine.connect() as connection:
                reset_empty_validation(connection,old,schema.REVISION)
                self.assertTrue(all(connection.scalar(sa.select(sa.func.count()).select_from(t))==0 for t in old.tables.values()),'验证库非空')
                connection.commit()
                operations=Operations(MigrationContext.configure(connection))
                # 仅在全空验证库移除本版本对象，反复演练同一次升级；不删除旧表或旧字段。
                for name in ('collection_batches','search_records'):
                    table,previous=new.tables[name],old.tables[name]
                    inspector=sa.inspect(connection)
                    for constraint in inspector.get_foreign_keys(name):
                        if constraint['name'] not in {c.name for c in previous.foreign_key_constraints}:
                            operations.drop_constraint(constraint['name'],name,type_='foreignkey')
                    for constraint in inspector.get_check_constraints(name):
                        if constraint['name'] not in {c.name for c in previous.constraints}:
                            operations.drop_constraint(constraint['name'],name,type_='check')
                    for index in sa.inspect(connection).get_indexes(name):
                        if index['name'] not in {i.name for i in previous.indexes} and any(c not in previous.c for c in index['column_names']):
                            operations.drop_index(index['name'],table_name=name)
                    for column in sa.inspect(connection).get_columns(name):
                        if column['name'] not in previous.c:
                            operations.drop_column(name,column['name'])
                connection.execute(sa.text('UPDATE alembic_version SET version_num=:v'),{'v':schema.REVISION})
                connection.commit()
            batch_key=uuid4().hex
            payload={'record':{'keyword':'样例','page_number':1,'schema_version':2,'scope':'first_page_loaded_cards'}}
            page_payload={'record':{'keyword':'样例','status':'results'}}
            b,p=old.tables['collection_batches'],old.tables['search_records']
            with engine.begin() as connection:
                batch_id=connection.execute(b.insert().values(platform='taobao',source_batch_key=batch_key,module='list',
                    status='limit_reached',scope_key='c'*64,coverage_status='limit_reached',visibility='published',
                    result_payload=payload)).inserted_primary_key[0]
                page_id=connection.execute(p.insert().values(record_type='search',keyword='样例',status='results',
                    source_url='https://s.taobao.com/search?q=冲突词',payload=page_payload)).inserted_primary_key[0]
            try:
                with self.assertRaisesRegex(ValueError,'冲突'):
                    mysql.upgrade_schema(settings)
                with engine.connect() as connection:
                    self.assertEqual(mysql.current_revision(connection),schema.REVISION)
                    self.assertNotIn('search_method',{c['name'] for c in sa.inspect(connection).get_columns('collection_batches')})
                with engine.begin() as connection:
                    connection.execute(p.update().where(p.c.id==page_id).values(source_url='https://s.taobao.com/search?q=样例'))
                injected=[]
                def fail_after_column(connection,cursor,statement,parameters,context,executemany):
                    """在第一个新列已由MySQL提交后中断，模拟不能整体回滚的DDL。"""
                    if not injected and 'ALTER TABLE collection_batches ADD COLUMN search_method' in statement:
                        injected.append(True)
                        raise RuntimeError('合成列创建后中断')
                sa.event.listen(engine,'after_cursor_execute',fail_after_column)
                try:
                    with patch.object(mysql,'create_mysql_engine',return_value=engine),self.assertRaisesRegex(RuntimeError,'列创建后'):
                        mysql.upgrade_schema(settings)
                finally:
                    sa.event.remove(engine,'after_cursor_execute',fail_after_column)
                self.assertTrue(injected)
                with engine.connect() as connection:
                    self.assertEqual(mysql.current_revision(connection),schema.REVISION)
                    self.assertIn('search_method',{c['name'] for c in sa.inspect(connection).get_columns('collection_batches')})
                injected=[]
                def fail_before_version(connection,cursor,statement,parameters,context,executemany):
                    """新列完成后在版本推进前故障，确认回填没有提前提交。"""
                    if not injected and statement.lstrip().upper().startswith('UPDATE ALEMBIC_VERSION') and (schema_v3.REVISION in statement or schema_v3.REVISION in str(parameters)):
                        injected.append(True)
                        raise RuntimeError('合成来源版本提交前中断')
                sa.event.listen(engine,'before_cursor_execute',fail_before_version)
                try:
                    with patch.object(mysql,'create_mysql_engine',return_value=engine),self.assertRaisesRegex(RuntimeError,'版本提交前'):
                        mysql.upgrade_schema(settings)
                finally:
                    sa.event.remove(engine,'before_cursor_execute',fail_before_version)
                self.assertTrue(injected)
                with engine.connect() as connection:
                    self.assertEqual(mysql.current_revision(connection),schema.REVISION)
                    self.assertIsNone(connection.scalar(sa.text('SELECT search_method FROM collection_batches WHERE id=:id'),{'id':batch_id}))
                    self.assertEqual(connection.scalar(sa.select(b.c.result_payload).where(b.c.id==batch_id)),payload)
                self.assertEqual(mysql.upgrade_schema(settings),mysql.REVISION)
                self.assertEqual(mysql.upgrade_schema(settings),mysql.REVISION)
                new=mysql.build_metadata()
                with engine.connect() as connection:
                    row=connection.execute(sa.select(new.tables['collection_batches']).where(new.tables['collection_batches'].c.id==batch_id)).mappings().one()
                    self.assertEqual((row['search_method'],row['search_keyword'],row['page_number']),('keyword','样例',1))
                    self.assertEqual(row['result_payload'],payload)
                    self.assertTrue(connection.scalar(sa.text('SELECT search_options IS NULL FROM collection_batches WHERE id=:id'),{'id':batch_id}))
                    row=connection.execute(sa.select(new.tables['search_records']).where(new.tables['search_records'].c.id==page_id)).mappings().one()
                    self.assertEqual(row['search_method'],'keyword')
                    self.assertIsNone(row['page_number'])
                    self.assertEqual(row['payload'],page_payload)
                self.assertEqual(mysql.inspect_schema(settings)['schema_errors'],[])
            finally:
                with engine.begin() as connection:
                    connection.execute(b.delete().where(b.c.id==batch_id))
                    connection.execute(p.delete().where(p.c.id==page_id))
                mysql.upgrade_schema(settings)
