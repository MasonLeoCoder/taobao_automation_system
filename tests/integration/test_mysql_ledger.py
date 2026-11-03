"""真实验证库账本与网页检查，合成记录全部事务回滚。"""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4
from decimal import Decimal
import os
import sqlalchemy as sa
from ecommerce.infrastructure.storage.mysql import create_mysql_engine
from ecommerce.infrastructure.storage.mysql_migrations.schema_v26 import build_metadata
from ecommerce.infrastructure.storage import ledger as repository
from ecommerce.modules.ledger import service
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app


@skipUnless(os.environ.get('ECOMMERCE_MYSQL_TESTS') == '1', '显式启用验证库')
class MySQLLedgerTests(TestCase):
    def setUp(self):
        """连接独立验证库并替换事务入口；参数：无；返回：无。"""
        target = replace(load_settings().mysql, database='taobao_automation_verify')
        self.engine = create_mysql_engine(target)
        self.addCleanup(self.engine.dispose)
        self.conn = self.engine.connect()
        self.addCleanup(self.conn.close)
        transaction = self.conn.begin()
        self.addCleanup(transaction.rollback)
        self.tables = build_metadata().tables
        @contextmanager
        def connection(settings, write=False):
            """共用回滚事务；参数：兼容仓储；返回：连接模型。"""
            yield self.conn, self.tables
        guard = patch.object(repository, 'connection', connection)
        guard.start()
        self.addCleanup(guard.stop)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = create_app(load_settings(root=Path(temporary.name)))
        self.app.testing = True
        self.app.config['SETTINGS'] = replace(self.app.config['SETTINGS'], collection_backend='mysql', mysql=target)
        self.client = self.app.test_client()
        self.category = self.conn.scalar(sa.select(self.tables['ledger_categories'].c.id).limit(1))

    def form(self, **changes):
        """生成明确测试表单；参数：覆盖值；返回：表单。"""
        return dict(id=uuid4().hex, revision='0', occurred_at='2040-10-28T12:34:56',
                    direction='expense', category_id=str(self.category), amount='600.25', note='验证库合成测试', **changes)

    def test_history_income_limit_void_idempotency_and_stale_edit(self):
        form = self.form()
        service.save_entry(None, form)
        service.save_entry(None, form)
        entry = repository.get_entry(None, form['id'])
        self.assertNotEqual(entry['created_at'], entry['occurred_at'])
        income = {**self.form(), 'direction':'income', 'amount':'800'}
        service.save_entry(None, income)
        service.save_limit(None, dict(month='2040-10', amount='500'))
        data = service.overview(None, '2040-10')
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['totals'], dict(expense=Decimal('600.25'), income=Decimal('800.00')))
        self.assertEqual(data['remaining'], Decimal('-100.25'))
        self.assertEqual(service.overview(None, '2040-11')['count'], 0)
        self.assertIsNone(service.overview(None, '2040-11')['budget'])
        service.save_entry(None, {**form, 'revision':'1', 'amount':'100'})
        with self.assertRaises(ValueError):
            service.save_entry(None, {**form, 'revision':'1', 'amount':'999'})
        repository.void_entry(None, form['id'], 2)
        self.assertEqual(service.overview(None, '2040-10')['totals']['expense'], Decimal('0'))
        self.assertEqual(service.overview(None, '2040-10', status='void')['count'], 1)

    def test_disabled_category_history_and_sql_constraints(self):
        form = self.form()
        service.save_entry(None, form)
        table = self.tables['ledger_categories']
        self.conn.execute(table.update().where(table.c.id == self.category).values(is_enabled=False))
        with self.assertRaises(ValueError):
            service.save_entry(None, self.form())
        service.save_entry(None, {**form, 'revision':'1', 'note':'保留原停用分类'})
        self.assertEqual(service.overview(None, '2040-10')['count'], 1)
        e = self.tables['ledger_entries']
        for values in (dict(amount=0), dict(direction='invalid'), dict(category_id=2147483647)):
            with self.assertRaises(sa.exc.DBAPIError), self.conn.begin_nested():
                self.conn.execute(e.update().where(e.c.id == form['id']).values(**values))

    def test_web_forms_security_errors_and_render(self):
        response = self.client.get('/plans/ledger?month=2040-10')
        self.assertEqual(response.status_code, 200)
        self.assertIn('账目发生时间', response.get_data(as_text=True))
        data = {**self.form(), 'month':'2040-10', 'action':'entry', 'token':self.app.config['COLLECTION_TOKEN']}
        self.assertEqual(self.client.post('/ledger/write', data=data).status_code, 303)
        self.assertEqual(self.client.post('/ledger/write', data={**data,'token':''}).status_code, 403)
        self.assertEqual(self.client.post('/ledger/write', data=data, headers={'Origin':'https://example.com'}).status_code, 403)
        self.assertEqual(self.client.post('/ledger/write', data={**data,'amount':'-1'}).status_code, 400)
        self.assertEqual(self.client.get('/plans/ledger?month=2040-10&edit='+data['id']).status_code, 200)
        self.assertEqual(self.client.get('/ledger/categories').status_code, 200)
        self.assertEqual(self.client.get('/plans/ledger?month=invalid').status_code, 400)

    def test_category_management_budget_zero_and_month_independence(self):
        form = dict(name='事务分类'+uuid4().hex[:8], sort_order='3', is_enabled='1')
        service.save_category(None, form)
        category = next(c for c in service.overview(None, '2040-10')['categories'] if c['name'] == form['name'])
        service.save_category(None, {**form, 'id':str(category['id']), 'name':form['name']+'改名', 'is_enabled':'0'})
        category = next(c for c in service.overview(None, '2040-10')['categories'] if c['id'] == category['id'])
        self.assertFalse(category['is_enabled'])
        service.save_limit(None, dict(month='2040-10', amount='1000'))
        service.save_limit(None, dict(month='2040-11', amount='800'))
        service.save_limit(None, dict(month='2040-10', amount='0'))
        self.assertEqual(service.overview(None, '2040-10')['budget'], Decimal('0'))
        self.assertEqual(service.overview(None, '2040-11')['budget'], Decimal('800'))

    def test_date_change_moves_month_and_notes_are_escaped(self):
        form = {**self.form(), 'note':'<script>alert(1)</script>'}
        service.save_entry(None, form)
        text = self.client.get('/plans/ledger?month=2040-10').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', text)
        self.assertIn('&lt;script&gt;', text)
        service.save_entry(None, {**form, 'revision':'1', 'occurred_at':'2040-11-01T00:00:00'})
        self.assertEqual(service.overview(None, '2040-10')['count'], 0)
        self.assertEqual(service.overview(None, '2040-11')['count'], 1)
        token = self.app.config['COLLECTION_TOKEN']
        self.assertEqual(self.client.post('/ledger/write', data=dict(action='entry',month='2040-10',token=token)).status_code, 400)
        self.assertEqual(self.client.post('/ledger/write', data=dict(token='错误令牌')).status_code, 403)

    def test_disabled_menu_blocks_all_ledger_actions(self):
        from ecommerce.web.workspace_plan import default_rows
        groups, items = default_rows()
        next(row for row in items if row['menu_key'] == 'ledger')['is_enabled'] = False
        self.app.config['WORKSPACE_MENU_DATABASE'] = True
        with patch('ecommerce.infrastructure.storage.web_menus.read', return_value=(groups, items)):
            for path in ('/plans/ledger', '/ledger/categories'):
                self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self.client.post('/ledger/write', data=dict(action='entry')).status_code, 403)
