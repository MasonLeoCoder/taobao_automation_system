"""验证淘宝买家账号切换和旧会话的保护边界。"""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from ecommerce.modules.account.web_buyers import BuyerAccountWorkbench
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import AutomationError, NeedsAttention, TaskStopped
from ecommerce.web.app import create_app


class BuyerAccountTests(TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.settings = load_settings(root=Path(temporary))
        self.workbench = BuyerAccountWorkbench(self.settings)
        self.workbench.state = {'phase': 'running'}
        self.current = {'id': 'a' * 32, 'session_id': 'b' * 32, 'login_name': '测试昵称甲',
                        'enabled': True, 'login_required': False, 'revision': 2}
        self.previous = self.workbench._profile(self.current['session_id']) / 'session.json'
        self.previous.parent.mkdir(parents=True)
        self.previous.write_text('old', encoding='utf-8')
        self.legacy = self.settings.for_platform('taobao').session_path
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_text('legacy', encoding='utf-8')
        self.addCleanup(lambda: self.assertEqual(self.legacy.read_text(encoding='utf-8'), 'legacy'))
        self.store = self.enterContext(patch('ecommerce.modules.account.web_buyers.AccountStore')).return_value
        self.session = self.enterContext(patch('ecommerce.modules.account.web_buyers.browser_session'))
        self.session.return_value.__enter__.return_value = Mock(), Mock()
        self.scan = self.enterContext(patch('ecommerce.modules.account.web_buyers.login_manually'))
        self.nick = self.enterContext(patch('ecommerce.modules.account.web_buyers.read_buyer_nickname',
                                            return_value=self.current['login_name']))
        self.save = self.enterContext(patch('ecommerce.modules.account.web_buyers.save_session', side_effect=self._save))

    def _save(self, _, settings):
        settings.session_path.parent.mkdir(parents=True)
        settings.session_path.write_text('new', encoding='utf-8')

    def test_wrong_nickname_never_saves_or_replaces_session(self):
        self.nick.return_value = '测试昵称乙'
        with self.assertRaisesRegex(NeedsAttention, '预期账号「测试昵称甲」，实际登录「测试昵称乙」'):
            self.workbench._inspect('login', self.current)
        self.save.assert_not_called()
        self.store.record_buyer.assert_not_called()
        self.assertEqual(self.previous.read_text(encoding='utf-8'), 'old')

    def test_cancel_and_save_failure_preserve_session(self):
        self.scan.side_effect = TaskStopped('取消扫码')
        with self.assertRaises(TaskStopped):
            self.workbench._inspect('login', self.current)
        self.scan.side_effect = None
        self.save.side_effect = AutomationError('模拟保存失败')
        with self.assertRaisesRegex(AutomationError, '模拟保存失败'):
            self.workbench._inspect('login', self.current)
        self.store.record_buyer.assert_not_called()
        self.assertEqual(self.previous.read_text(encoding='utf-8'), 'old')

    def test_duplicate_drops_candidate_and_success_references_new_directory(self):
        self.store.record_buyer.side_effect = NeedsAttention('此账号已登记')
        with self.assertRaisesRegex(NeedsAttention, '已登记'):
            self.workbench._inspect('add', None)
        self.assertFalse(self.save.call_args.args[1].session_path.parent.exists())
        self.store.record_buyer.side_effect = None
        self.store.cleanup_sessions.return_value = False
        self.workbench._inspect('login', self.current)
        nickname, session_id = self.store.record_buyer.call_args.args
        self.assertEqual(nickname, self.current['login_name'])
        self.assertNotEqual(session_id, self.current['session_id'])
        self.assertEqual((self.workbench._profile(session_id) / 'session.json').read_text(), 'new')
        self.store.cleanup_sessions.assert_called_once()

    def test_check_marks_explicit_logout_and_preserves_session_on_unknown(self):
        with (patch('ecommerce.modules.account.web_buyers.check_login', return_value=False),
              patch('ecommerce.modules.account.web_buyers.is_login_required', return_value=False) as expired):
            with self.assertRaisesRegex(NeedsAttention, '核验未完成'):
                self.workbench._inspect('check', self.current)
            self.store.change_state.assert_not_called()
            expired.return_value = True
            with self.assertRaisesRegex(NeedsAttention, '重新扫码'):
                self.workbench._inspect('check', self.current)
        self.store.change_state.assert_called_once_with(self.current, 'require_login')
        self.assertEqual(self.previous.read_text(encoding='utf-8'), 'old')

    def test_flagged_account_can_check_and_reuse_its_original_session(self):
        self.current['login_required'] = True
        self.workbench.state = None
        with (patch.object(self.workbench, '_account', return_value=self.current),
              patch('ecommerce.modules.account.web_accounts.Thread') as thread):
            self.workbench.start('check', self.current['id'], revision=2)
            thread.return_value.start.assert_called_once()
        with patch('ecommerce.modules.account.web_buyers.check_login', return_value=True):
            self.workbench._inspect('check', self.current)
        self.store.record_buyer.assert_called_once_with(self.current['login_name'], self.current['session_id'], current=self.current)
        self.scan.assert_not_called()
        self.assertTrue(self.previous.is_file())

    def test_buyer_page_and_requests_do_not_use_seller_store_paths(self):
        app = create_app(self.settings)
        app.config['SETTINGS'] = replace(self.settings, task_backend='mysql')
        client = app.test_client()
        row = {**self.current, 'login_required': True, 'verified_at': datetime(2026, 11, 15), 'cleanup_pending': False}
        seller = app.extensions['platform_accounts']
        buyer = app.extensions['taobao_buyer_accounts']
        headers = {'X-Collection-Token': app.config['COLLECTION_TOKEN']}
        path = '/api/accounts/start?platform=taobao'
        payload = {'action': 'check', 'account_id': self.current['id'], 'revision': 2}
        with (patch.object(seller, 'accounts') as seller_accounts,
              patch.object(buyer, 'accounts', return_value=[row]),
              patch.object(buyer, 'start', return_value={'phase': 'running'}) as start):
            html = client.get('/accounts?platform=taobao').get_data(as_text=True)
            self.assertIn('测试昵称甲', html)
            self.assertIn('data-account-action="check"', html)
            self.assertNotIn('data-shop-id', html)
            self.assertEqual(client.post(path, json=payload).status_code, 403)
            self.assertEqual(client.post(path, json={**payload, 'shop_id': '123'}, headers=headers).status_code, 422)
            self.assertEqual(client.post(path, json=payload, headers=headers).status_code, 202)
            start.assert_called_once_with('check', self.current['id'], None, 2)
            seller_accounts.assert_not_called()
