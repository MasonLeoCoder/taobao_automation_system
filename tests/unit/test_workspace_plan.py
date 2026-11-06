"""工作台规划页只读边界、原入口兼容和当前菜单归属。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from flask import render_template
from ecommerce.shared.config import load_settings
from ecommerce.web.app import create_app
from ecommerce.web.workspace_plan import ITEMS, workspace_navigation


class WorkspacePlanTests(TestCase):
    def setUp(self):
        """建立临时文件后端；参数：无；返回：无。"""
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = create_app(load_settings(root=Path(temp.name)))
        self.app.testing = True
        self.client = self.app.test_client()

    def test_plans_do_not_query_or_execute_business_and_only_accept_read(self):
        """所有规划页不读取数据库、不调用AI或派发任务，POST不能误触业务。"""
        targets = ('ecommerce.modules.collection.queries.query',
                   'ecommerce.modules.ai_service.check_connection',
                   'ecommerce.infrastructure.tasks.runner.run_task',
                   'ecommerce.modules.collection.dispatch.on_web_start')
        mocks = []
        for target in targets:
            guard = patch(target, side_effect=AssertionError('规划页不能执行或查询业务'))
            mocks.append(guard.start())
            self.addCleanup(guard.stop)
        for item in ITEMS.values():
            if not item['planned'] or item['key'] == 'listing':
                continue
            response = self.client.get('/plans/' + item['key'])
            self.assertEqual(response.status_code, 200, item['key'])
            text = response.get_data(as_text=True)
            self.assertIn('已纳入规划，尚未建设', text)
            self.assertIn(item['problem'], text)
            self.assertNotIn('<form', text)
            self.assertEqual(text.count('aria-current="page"'), 1)
            self.assertEqual(self.client.post('/plans/' + item['key']).status_code, 405)
        for mock in mocks:
            mock.assert_not_called()

    def test_unknown_and_existing_menu_ids_are_not_planning_pages(self):
        """错误规划标识返回404，不能用规划页面替代已有入口。"""
        for key in ('unknown', 'catalog', 'home', 'tasks', 'collection'):
            self.assertEqual(self.client.get('/plans/' + key).status_code, 404)

    def test_original_menu_addresses_are_preserved(self):
        """十个已有入口仍生成原访问地址，没有指向规划页。"""
        expected = {'home': '/', 'collection': '/modules/collection',
                    'searches': '/collections/searches', 'catalog': '/collections/details',
                    'analysis': '/analysis/searches', 'favorites': '/favorites',
                    'tools': '/tools', 'tasks': '/tasks', 'logs': '/logs', 'ai': '/settings/ai', 'ledger': '/plans/ledger'}
        with self.app.test_request_context('/'):
            actual = {item['key']: item['href'] for group in workspace_navigation('home')
                      for item in group['items'] if not item['planned']}
        self.assertEqual(actual, expected)

    def test_nested_pages_and_legacy_query_keep_one_current_menu(self):
        """保留搜索、批次和商品页的原归属，规划与工具子页也只高亮一项。"""
        cases = (('/collections?view=searches', 'collections', 'searches'),
                 ('/collections/batches/9', 'collections', 'searches'),
                 ('/collections/searches/products', 'collections', 'searches'),
                 ('/collections/products/123?module=composite', 'collections', 'catalog'),
                 ('/tools/youtube', 'tools', 'tools'),
                 ('/settings/ai', 'ai', 'ai'),
                 ('/plans/ledger', 'plan:ledger', 'ledger'))
        for path, active, expected in cases:
            with self.app.test_request_context(path):
                groups = workspace_navigation(active)
                self.assertEqual(sum(group['current'] for group in groups), 1)
                selected = [item['key'] for group in groups for item in group['items'] if item['current']]
                self.assertEqual(selected, [expected])
                markup = render_template('workspace_sidebar.html', active=active)
                self.assertEqual(markup.count('aria-current="page"'), 1)
                self.assertEqual(markup.count('data-current-group open'), 1)

    def test_ledger_and_listing_explain_scope_without_fake_actions(self):
        """账本与填写页明确实际用途、已有能力及尚未建设范围。"""
        self.assertEqual(self.client.get('/plans/ledger').status_code, 409)
        text = self.client.get('/plans/listing').get_data(as_text=True)
        self.assertIn('选择商品文件夹，自检通过后', text)
        self.assertIn('填写完成后直接提交到仓库', text)
        self.assertIn('webkitdirectory', text)
        self.assertEqual(self.client.post('/plans/listing').status_code, 405)
