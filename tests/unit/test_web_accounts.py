from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ecommerce.modules.account.web_accounts import AccountWorkbench
from ecommerce.platforms.qianniu.identity import ShopIdentity
from ecommerce.shared.config import load_settings
from ecommerce.platforms.qianniu.login import LoginStatus
from ecommerce.shared.exceptions import NeedsAttention


class AccountWorkbenchTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workbench = AccountWorkbench(load_settings(root=Path(temporary.name)))
        self.current = {
            'id': 'a' * 32, 'session_id': 'b' * 32,
            'login_name': 'seller:示例', 'platform_shop_id': '123456789',
            'enabled': True, 'login_required': False, 'revision': 2,
        }
        old_session = self.workbench._profile(self.current['session_id'])
        old_session.mkdir(parents=True)
        self.saved = old_session / 'session.json'
        self.saved.write_text('previous session', encoding='utf-8')

    def login_with_identity(self, identity, *, failure=None):
        def save(_context, settings):
            settings.session_path.parent.mkdir(parents=True)
            settings.session_path.write_text('verified session', encoding='utf-8')

        with patch('ecommerce.modules.account.web_accounts.AccountStore') as store, \
                patch('ecommerce.modules.account.web_accounts.browser_session') as session, \
                patch('ecommerce.modules.account.web_accounts.login_with_qrcode'), \
                patch('ecommerce.modules.account.web_accounts.read_shop_identity', return_value=identity), \
                patch('ecommerce.modules.account.web_accounts.save_session', side_effect=save):
            session.return_value.__enter__.return_value = SimpleNamespace(), SimpleNamespace()
            store.return_value.cleanup_sessions.side_effect = lambda _account_id, remove: not remove(self.current['session_id'])
            self.workbench.state = {'phase': 'running'}
            if failure:
                with self.assertRaisesRegex(NeedsAttention, failure):
                    self.workbench._inspect('login', self.current)
                store.return_value.record.assert_not_called()
            else:
                self.workbench._inspect('login', self.current)
                store.return_value.record.assert_called_once()
                _, identifier = store.return_value.record.call_args.args
                self.assertEqual((self.workbench._profile(identifier) / 'session.json').read_text(),
                                 'verified session')

    def test_wrong_account_does_not_replace_previous_session(self):
        self.login_with_identity(ShopIdentity('123456789', '示例店铺', 'seller', 'seller'), failure='保留原会话')
        self.assertEqual(self.saved.read_text(), 'previous session')

    def test_correct_account_in_wrong_shop_preserves_previous_session(self):
        self.login_with_identity(ShopIdentity('987654321', '另一家店', '示例-运营', 'seller:示例'),
                                 failure='保留原会话')
        self.assertEqual(self.saved.read_text(), 'previous session')

    def test_verified_account_can_replace_session(self):
        self.login_with_identity(ShopIdentity('123456789', '示例店铺', '示例-运营', 'seller:示例'))
        self.assertFalse(self.saved.exists())

    def test_stale_request_and_disabled_account_cannot_open_browser(self):
        with patch('ecommerce.modules.account.web_accounts.AccountStore') as store, \
                patch('ecommerce.modules.account.web_accounts.Thread') as thread:
            self.current['enabled'] = False
            store.return_value.list_accounts.return_value = [self.current]
            with self.assertRaisesRegex(NeedsAttention, '状态已变化'):
                self.workbench.start('clear', self.current['id'], self.current['platform_shop_id'], 1)
            with self.assertRaisesRegex(NeedsAttention, '账号已停用'):
                self.workbench.start('login', self.current['id'], self.current['platform_shop_id'], 2)
            thread.assert_not_called()

    def test_login_check_records_only_explicit_expiration(self):
        for status in (LoginStatus.UNCONFIRMED, LoginStatus.REQUIRED):
            with self.subTest(status=status), patch('ecommerce.modules.account.web_accounts.AccountStore') as store, \
                    patch('ecommerce.modules.account.web_accounts.browser_session') as session, \
                    patch('ecommerce.modules.account.web_accounts.inspect_login', return_value=status):
                self.workbench.state = {}
                session.return_value.__enter__.return_value = SimpleNamespace(), SimpleNamespace()
                with self.assertRaises(NeedsAttention):
                    self.workbench._inspect('check', self.current)
                if status is LoginStatus.REQUIRED:
                    store.return_value.change_state.assert_called_once_with(self.current, 'require_login')
                else:
                    store.return_value.change_state.assert_not_called()

    def test_incomplete_cleanup_keeps_retry_available(self):
        with patch('ecommerce.modules.account.web_accounts.AccountStore') as store:
            self.workbench.state = {}
            store.return_value.cleanup_sessions.return_value = True
            message, pending = self.workbench._manage('clear', self.current)
            self.assertTrue(pending)
            self.assertIn('重试', message)
            store.return_value.change_state.assert_called_once_with(self.current, 'clear')
            store.return_value.cleanup_sessions.return_value = False
            self.assertEqual(self.workbench._manage('retry_cleanup', self.current), ('已清理本机登录资料', False))
            store.return_value.change_state.assert_called_once()
