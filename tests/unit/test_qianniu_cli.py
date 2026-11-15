"""千牛终端入口必须绑定已登记的店铺和账号。"""
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from ecommerce.cli.app import main
from ecommerce.cli.commands import account, product
from ecommerce.modules.account.managed import account_session_directory
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention


class QianniuCliTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = replace(load_settings(root=Path(directory.name)), task_backend='mysql')
        self.row = dict(id='a' * 32, shop_id='b' * 32, session_id='c' * 32, enabled=True,
                        login_required=False, login_name='tb123:子账号', platform_shop_id='123', shop_name='测试店铺')
        self.session_dir = account_session_directory(self.settings.workspace, self.row['session_id'])
        self.session_dir.mkdir(parents=True)
        (self.session_dir / 'session.json').write_text('{}', encoding='utf-8')
        store = self.enterContext(patch('ecommerce.modules.account.managed.AccountStore'))
        store.return_value.list_accounts.return_value = [self.row]

    def test_all_qianniu_entrypoints_require_explicit_account_and_shop(self):
        for args in (['account', 'check'], ['account', 'login', '--shop-id', '123'],
                     ['product', 'open', '--account', 'tb123:子账号'], ['upload']):
            with self.subTest(args=args), redirect_stderr(StringIO()) as message:
                with self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)
                self.assertIn('--shop-id 和 --account', message.getvalue())

    def test_cross_shop_account_stops_before_opening_browser(self):
        with patch.object(product, 'run_task') as run, self.assertRaisesRegex(NeedsAttention, '未登记'):
            product.execute(self.settings, False, False, shop_id='456', account=self.row['login_name'])
        run.assert_not_called()

    def test_product_navigation_opens_selected_session_under_original_lock(self):
        with (patch.object(product, 'run_task', side_effect=lambda settings, name, work, **kwargs: work()) as run,
              patch('ecommerce.modules.product.service.open_publish', return_value='已打开') as publish):
            result = product.execute(self.settings, False, False, shop_id='123', account=self.row['login_name'])
        self.assertEqual(result, '已打开')
        self.assertEqual(run.call_args.args[0].profile_path, self.settings.for_platform('qianniu').profile_path)
        self.assertEqual(run.call_args.kwargs['qianniu_scope']['account_id'], self.row['id'])
        self.assertEqual(publish.call_args.args[0].profile_path, self.session_dir / 'browser')
        self.assertEqual(publish.call_args.kwargs['qianniu_target']['shop_id'], self.row['shop_id'])

    def test_only_explicit_login_can_select_account_needing_relogin(self):
        self.row['login_required'] = True
        self.row['session_id'] = None
        with self.assertRaisesRegex(NeedsAttention, '需重新登录'):
            account.execute(self.settings, 'check', shop_id='123', account=self.row['login_name'])
        with (patch.object(account, 'run_task', side_effect=lambda settings, name, work, **kwargs: work()) as run,
              patch('ecommerce.modules.account.web_accounts.AccountWorkbench') as workbench):
            account.execute(self.settings, 'login', shop_id='123', account=self.row['login_name'])
        self.assertEqual(run.call_args.kwargs['qianniu_scope']['login_name'], self.row['login_name'])
        self.assertEqual(workbench.return_value.inspect_selected.call_args.args, ('login', self.row))
