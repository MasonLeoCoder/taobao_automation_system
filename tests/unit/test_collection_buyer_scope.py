"""验证淘宝身份核验与共用队列键的执行边界。"""
from unittest import TestCase
from unittest.mock import Mock, patch

from ecommerce.modules.account import buyers
from ecommerce.shared.exceptions import BuyerUnavailable


class CollectionBuyerScopeTests(TestCase):
    def test_environment_lock_stays_fixed_and_browser_uses_selected_profile(self):
        settings, browser, row = Mock(), Mock(), {'id':'a' * 32, 'login_name':'买家甲'}
        buyer = {'account_id':row['id'], 'login_name':row['login_name']}
        def runner(actual, name, action, **options):
            self.assertIs(actual, settings)
            self.assertEqual(options['buyer_scope'], buyer)
            options['prepare']()
            return action()
        with (patch.object(buyers,'resolve',return_value=(browser,row)), patch.object(buyers,'probe')):
            result = buyers.run(settings,'collection.taobao.collect',lambda: buyers.browser_settings(settings),
                                buyer=buyer,runner=runner)
        self.assertIs(result,browser)
        self.assertIs(buyers.browser_settings(settings),settings)

    def test_wrong_identity_cannot_emit_verified_event_or_save_session(self):
        settings, page, session = Mock(), Mock(), Mock()
        row = {'id':'a' * 32, 'login_name':'买家甲'}
        with (patch.object(buyers,'check_login',return_value=True),
              patch.object(buyers,'read_buyer_nickname',return_value='买家乙'),
              patch.object(buyers,'save_session') as save, patch.object(buyers,'current_events',return_value=session)):
            with self.assertRaisesRegex(BuyerUnavailable,'实际为「买家乙」'):
                buyers.authenticate(settings,row,page)
        save.assert_not_called()
        session.append.assert_not_called()

    def test_matching_nickname_creates_barrier_and_sets_login_loss_handler(self):
        settings, page, session, guard = Mock(), Mock(), Mock(), Mock()
        row = {'id':'a' * 32, 'login_name':'买家甲'}
        with (patch.object(buyers,'check_login',return_value=True),
              patch.object(buyers,'read_buyer_nickname',return_value='买家甲'), patch.object(buyers,'save_session'),
              patch.object(buyers,'current_events',return_value=session), patch.object(buyers,'active_guard',return_value=guard),
              patch.object(buyers,'storage_checkpoint') as checkpoint, patch.object(buyers,'recheck_login') as recheck):
            self.assertIs(buyers.authenticate(settings,row,page),page)
            guard.on_account_login_required()
        details = session.operation.call_args.kwargs['details']
        self.assertEqual((details['account_id'],details['login_name']),(row['id'],row['login_name']))
        self.assertIs(session.collection_barrier,session.append.return_value)
        checkpoint.assert_called_once()
        recheck.assert_called_once_with(settings,row,guard)

    def test_recheck_marks_only_an_explicit_logout(self):
        from contextlib import nullcontext
        settings, page, guard = Mock(timeout_ms=1000), Mock(), Mock()
        row = {'id':'a' * 32, 'login_name':'买家甲'}
        guard.login_probe_page.return_value = nullcontext(page)
        with (patch.object(buyers,'check_login',return_value=True) as login,
              patch.object(buyers,'is_login_required',return_value=False) as required,
              patch.object(buyers,'read_buyer_nickname',return_value='买家甲') as nickname,
              patch.object(buyers,'AccountStore') as store):
            buyers.recheck_login(settings,row,guard)
            nickname.return_value = '买家乙'
            with self.assertRaisesRegex(BuyerUnavailable,'复核为「买家乙」'):
                buyers.recheck_login(settings,row,guard)
            login.return_value = False
            with self.assertRaisesRegex(BuyerUnavailable,'未能复核'):
                buyers.recheck_login(settings,row,guard)
            store.return_value.change_state.assert_not_called()
            required.return_value = True
            with self.assertRaisesRegex(BuyerUnavailable,'已要求登录'):
                buyers.recheck_login(settings,row,guard)
            store.return_value.change_state.assert_called_once_with(row,'require_login')
