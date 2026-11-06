"""数据库菜单的刷新、继承与路由守卫；仅使用临时数据库和模拟业务。"""
from copy import copy
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import re
import sqlalchemy as sa
from flask import jsonify
from sqlalchemy.pool import StaticPool
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app
from ecommerce.web.workspace_plan import menu_data, workspace_navigation
from ecommerce.web.menu_access import requirements
from ecommerce.infrastructure.storage.mysql import MySQLStorageError
from ecommerce.infrastructure.storage.mysql_migrations.schema_v25 import build_metadata, NEW_TABLES

MIGRATION = import_module('ecommerce.infrastructure.storage.mysql_migrations.versions.0025_web_menu')
CATEGORY_MIGRATION = import_module('ecommerce.infrastructure.storage.mysql_migrations.versions.0027_publish_categories')
SYCM_MIGRATION = import_module('ecommerce.infrastructure.storage.mysql_migrations.versions.0028_sycm_search')


class MenuDatabaseTests(TestCase):
    def setUp(self):
        """建立临时菜单表并使用真实只读仓储；参数：无；返回：无，不接触正式数据库。"""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = create_app(load_settings(root=Path(temporary.name)))
        self.app.testing = True
        self.app.config['WORKSPACE_MENU_DATABASE'] = True
        self.client = self.app.test_client()
        self.engine = sa.create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
        self.addCleanup(self.engine.dispose)
        metadata = sa.MetaData()
        for name in NEW_TABLES:
            columns = []
            for column in build_metadata().tables[name].c:
                kind = copy(column.type)
                if hasattr(kind, 'collation'):
                    kind.collation = None
                default = sa.text('CURRENT_TIMESTAMP') if isinstance(kind, sa.DateTime) else column.server_default
                columns.append(sa.Column(column.name, kind, primary_key=column.primary_key,
                                         nullable=column.nullable, server_default=default))
            sa.Table(name, metadata, *columns)
        metadata.create_all(self.engine)
        self.tables = metadata.tables
        with self.engine.begin() as connection:
            MIGRATION.seed_missing(connection)
            CATEGORY_MIGRATION.seed_menu(connection, self.tables['web_menu_items'])
            SYCM_MIGRATION.seed_menu(connection, self.tables['web_menu_items'])
            connection.execute(self.tables['web_menu_items'].update().where(self.tables['web_menu_items'].c.menu_key == 'ledger').values(construction_status='available'))
        mocked = patch('ecommerce.infrastructure.storage.web_menus.task_store',
                       return_value=SimpleNamespace(engine=self.engine, tables=self.tables))
        self.store = mocked.start()
        self.addCleanup(mocked.stop)

    def change(self, key, *, group=False, **values):
        """修改本测试配置；参数：标识、分组标记及字段；返回：无。"""
        table = self.tables['web_menu_groups' if group else 'web_menu_items']
        with self.engine.begin() as connection:
            connection.execute(table.update().where(table.c.group_key == key if group else table.c.menu_key == key).values(**values))

    def stub(self, endpoint):
        """替换真实业务并记录是否执行；参数：端点；返回：模拟函数。"""
        action = Mock(side_effect=lambda **kwargs: jsonify(ok=True))
        original = self.app.view_functions[endpoint]
        self.app.view_functions[endpoint] = action
        self.addCleanup(self.app.view_functions.__setitem__, endpoint, original)
        return action

    def nav(self):
        """读取独立请求的菜单；参数：无；返回：六组有效菜单。"""
        with self.app.test_request_context('/plans/business-data'):
            return workspace_navigation('plan:business-data')

    def test_names_group_order_and_plan_title_refresh_without_restart(self):
        self.change('research', group=True, name='市场研究')
        self.change('business-data', name='收支管理', group_key='research', sort_order=0)
        response = self.client.get('/plans/business-data')
        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<h1>收支管理</h1>', text)
        self.assertIn('市场研究 / 收支管理', text)
        group = next(group for group in self.nav() if group['key'] == 'research')
        self.assertEqual(group['items'][0]['key'], 'business-data')
        self.assertEqual(group['items'][0]['href'], '/plans/business-data')
        self.change('business-data', name='真实账本')
        self.assertIn('<h1>真实账本</h1>', self.client.get('/plans/business-data').get_data(as_text=True))

    def test_one_snapshot_per_request_but_no_cross_request_cache(self):
        with patch('ecommerce.infrastructure.storage.web_menus.read', wraps=__import__(
                'ecommerce.infrastructure.storage.web_menus', fromlist=['read']).read) as reader:
            self.client.get('/plans/business-data')
            self.assertEqual(reader.call_count, 1)
            self.client.get('/plans/business-data')
            self.assertEqual(reader.call_count, 2)

    def test_hide_and_disable_are_independent_and_parent_retains_child_setting(self):
        self.change('business-data', is_visible=False)
        self.assertEqual(self.client.get('/plans/business-data').status_code, 200)
        self.assertFalse(any(item['key'] == 'business-data' for group in self.nav() for item in group['items']))
        self.change('operations', group=True, is_enabled=False)
        response = self.client.get('/plans/business-data')
        self.assertEqual(response.status_code, 403)
        self.assertIn('所属分组“店铺经营”已停用', response.get_data(as_text=True))
        self.change('operations', group=True, is_enabled=True)
        self.assertEqual(self.client.get('/plans/business-data').status_code, 200)
        self.change('operations', group=True, is_visible=False)
        self.assertFalse(any(group['key'] == 'operations' for group in self.nav()))
        self.assertEqual(self.client.get('/plans/business-data').status_code, 200)

    def test_empty_group_is_hidden_and_disabled_related_links_are_removed(self):
        with self.engine.begin() as connection:
            table = self.tables['web_menu_items']
            connection.execute(table.update().where(table.c.group_key == 'operations').values(is_visible=False))
        self.assertFalse(any(group['key'] == 'operations' for group in self.nav()))
        self.change('catalog', is_enabled=False)
        response = self.client.get('/plans/listing')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('href="/collections/details"', response.get_data(as_text=True))

    def test_catalog_disable_blocks_nested_pages_analysis_and_download_interfaces(self):
        self.change('catalog', is_enabled=False)
        cases = (('GET', '/collections/products/123', 'pages.collection_product'),
                 ('GET', '/analysis/reviews/tasks/task', 'review_analysis.result'),
                 ('GET', '/analysis/images/main_images/tasks/task', 'image_modules.result'),
                 ('POST', '/api/analysis/questions/products/123', 'question_analysis.start'),
                 ('POST', '/api/tools/images/products/123/batches/1/download', 'image_tools.main_images'))
        for method, path, endpoint in cases:
            action = self.stub(endpoint)
            response = self.client.open(path, method=method, json={})
            self.assertEqual(response.status_code, 403, path)
            action.assert_not_called()

    def test_collection_disable_blocks_product_update_and_resume(self):
        self.change('collection', is_enabled=False)
        for path, endpoint in (('/api/collection-jobs', 'collection_jobs.create'),
                               ('/api/collection-jobs/task/resume', 'collection_jobs.resume_job'),
                               ('/api/collections/products/123/module-update/skus', 'collection_jobs.update_module')):
            action = self.stub(endpoint)
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
            action.assert_not_called()

    def test_safety_status_stop_and_pause_keep_original_token_checks(self):
        with self.engine.begin() as connection:
            connection.execute(self.tables['web_menu_groups'].update().values(is_enabled=False))
        status = self.stub('collection_jobs.job_status')
        self.assertEqual(self.client.get('/api/collection-jobs/task').status_code, 200)
        status.assert_called_once()
        control = self.stub('collection_jobs.job_control')
        headers = {'X-Collection-Token': self.app.config['COLLECTION_TOKEN']}
        self.assertEqual(self.client.post('/api/collection-jobs/task/control', json={'action': 'stop'}).status_code, 403)
        control.assert_not_called()
        self.assertEqual(self.client.post('/api/collection-jobs/task/control', json={'action': 'stop'}, headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/collection-jobs/task/control', json={'action': 'continue'}, headers=headers).status_code, 403)
        with patch('ecommerce.infrastructure.storage.web_menus.read', side_effect=MySQLStorageError('合成断库')):
            self.assertEqual(self.client.post('/api/collection-jobs/task/control', json={'action': 'stop'}, headers=headers).status_code, 200)
            self.assertEqual(self.client.get('/health').status_code, 200)

    def test_configuration_failure_missing_key_and_wrong_construction_fail_closed(self):
        action = self.stub('collection_jobs.create')
        with patch('ecommerce.infrastructure.storage.web_menus.read', side_effect=MySQLStorageError('合成断库')):
            self.assertEqual(self.client.get('/plans/business-data').status_code, 503)
            self.assertEqual(self.client.post('/api/collection-jobs', json={}).status_code, 503)
        action.assert_not_called()
        self.change('business-data', construction_status='available')
        self.assertEqual(self.client.get('/plans/business-data').status_code, 503)
        self.change('business-data', construction_status='planned')
        with self.engine.begin() as connection:
            table = self.tables['web_menu_items']
            connection.execute(table.delete().where(table.c.menu_key == 'business-data'))
        self.assertEqual(self.client.get('/').status_code, 503)

    def test_shared_resource_ownership_cannot_bypass_disabled_catalog(self):
        self.change('catalog', is_enabled=False)
        action = self.stub('pages.collection_records')
        with patch('ecommerce.web.menu_access.web_menus.resource_owners', return_value={'catalog'}):
            self.assertEqual(self.client.get('/api/collections/batches/1/records').status_code, 403)
            action.assert_not_called()
        with patch('ecommerce.web.menu_access.web_menus.resource_owners', return_value={'searches'}):
            self.assertEqual(self.client.get('/api/collections/batches/1/records').status_code, 200)

    def test_seed_missing_does_not_override_user_changes(self):
        self.change('business-data', name='用户账本', sort_order=7, is_enabled=False)
        with self.engine.begin() as connection:
            MIGRATION.seed_missing(connection)
            row = connection.execute(sa.select(self.tables['web_menu_items']).where(
                self.tables['web_menu_items'].c.menu_key == 'business-data')).mappings().one()
            self.assertEqual((row['name'], row['sort_order'], row['is_enabled']), ('用户账本', 7, False))

    def test_shared_file_owners_keep_list_thumbnails_but_not_disabled_detail_data(self):
        """分析与关注可继续显示已发布列表图，不能借此读取停用的详情批次。"""
        metadata = self.tables['web_menu_items'].metadata
        batches = sa.Table('collection_batches', metadata, sa.Column('id', sa.Integer, primary_key=True),
                           sa.Column('module', sa.String), sa.Column('visibility', sa.String))
        media = sa.Table('media_references', metadata, sa.Column('id', sa.Integer, primary_key=True),
                         sa.Column('batch_id', sa.Integer), sa.Column('file_asset_id', sa.Integer))
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(batches.insert(), [dict(id=1, module='list', visibility='published'),
                                                 dict(id=2, module='main_images', visibility='published')])
            connection.execute(media.insert(), [dict(id=1, batch_id=1, file_asset_id=11), dict(id=2, batch_id=2, file_asset_id=22)])
        from ecommerce.infrastructure.storage.web_menus import resource_owners
        settings = self.app.config['SETTINGS'].mysql
        self.assertEqual(resource_owners(settings, batch_id=1), {'searches'})
        self.assertEqual(resource_owners(settings, batch_id=2), {'catalog'})
        self.assertEqual(resource_owners(settings, file_id=11), {'searches', 'analysis', 'favorites', 'catalog'})
        self.assertEqual(resource_owners(settings, file_id=22), {'catalog'})

    def test_every_registered_business_endpoint_has_a_policy(self):
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint in ('static', 'pages.health'):
                continue
            path = re.sub(r'<(?:int:)?[^>]+>', '1', str(rule))
            path = path.replace('/plans/1', '/plans/business-data')
            method = 'GET' if 'GET' in rule.methods else 'POST'
            with self.app.test_request_context(path, method=method, json={}):
                self.assertIsInstance(requirements(menu_data()), tuple, rule.endpoint)
