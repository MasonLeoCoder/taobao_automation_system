"""验证淘宝身份核验与共用队列键的执行边界。"""
from contextlib import ExitStack, contextmanager
from unittest import TestCase
from unittest.mock import Mock, patch

from ecommerce.modules.account import buyers
from ecommerce.shared.exceptions import BuyerUnavailable


class CollectionBuyerScopeTests(TestCase):
    def test_environment_lock_stays_fixed_and_browser_uses_selected_profile(self):
        settings, browser, row = Mock(), Mock(), {'id':'a' * 32, 'login_name':'买家甲'}
        buyer = {'account_id':row['id'], 'login_name':row['login_name']}
        opened = Mock(), Mock()
        stages = []

        @contextmanager
        def connect(actual):
            self.assertIs(actual, browser)
            stages.append('open')
            yield opened
            stages.append('close')

        def runner(actual, name, action, **options):
            self.assertIs(actual, settings)
            self.assertEqual(options['buyer_scope'], buyer)
            with ExitStack() as resources:
                options['prepare'](resources)
                return action()

        def collect():
            stages.append('collect')
            return buyers.collection_browser(settings, on_attention=attention)

        attention = Mock()
        with (patch.object(buyers,'resolve',return_value=(browser,row)),
              patch.object(buyers,'open_browser',side_effect=connect) as launch,
              patch.object(buyers,'probe') as probe, patch.object(buyers,'browser_session') as guard):
            result = buyers.run(settings,'collection.taobao.collect',collect,
                                buyer=buyer,runner=runner)
            self.assertIs(result,guard.return_value)
            self.assertEqual(stages,['open','collect','close'])
            launch.assert_called_once_with(browser)
            probe.assert_called_once_with(browser,row,opened)
            guard.assert_called_once_with(browser,on_attention=attention,opened=opened)
        self.assertIsNone(buyers.current())

    def test_expired_preflight_closes_browser_before_creating_execution(self):
        settings, selected, page = Mock(), Mock(), Mock()
        row = {'id':'a' * 32, 'login_name':'买家甲'}
        action, closed = Mock(), Mock()

        @contextmanager
        def connect(actual):
            try:
                yield Mock(), page
            finally:
                closed()

        def runner(actual, name, work, **options):
            with ExitStack() as resources:
                options['prepare'](resources)
                return work()

        from contextlib import nullcontext
        with (patch.object(buyers,'resolve',return_value=(selected,row)),
              patch.object(buyers,'open_browser',side_effect=connect),
              patch.object(buyers,'browser_session',side_effect=lambda *args, opened, **kwargs: nullcontext(opened)),
              patch.object(buyers,'check_login',return_value=False),
              patch.object(buyers,'is_login_required',return_value=True), patch.object(buyers,'AccountStore') as store):
            with self.assertRaisesRegex(BuyerUnavailable,'重新登录原账号'):
                buyers.run(settings,'collection.taobao.collect',action,buyer={'account_id':row['id']},runner=runner)
        closed.assert_called_once()
        action.assert_not_called()
        store.return_value.change_state.assert_called_once_with(row,'require_login')

    def test_wrong_identity_cannot_emit_verified_event_or_save_session(self):
        settings, page, session = Mock(), Mock(), Mock()
        row = {'id':'a' * 32, 'login_name':'买家甲'}
        with (patch.object(buyers,'is_authenticated',return_value=True), patch.object(buyers,'check_login') as navigate,
              patch.object(buyers,'read_buyer_nickname',return_value='买家乙'),
              patch.object(buyers,'save_session') as save, patch.object(buyers,'current_events',return_value=session),
              patch.object(buyers,'emit_event') as event):
            with self.assertRaisesRegex(BuyerUnavailable,'实际为「买家乙」'):
                buyers.authenticate(settings,row,page,reuse_home=True)
        navigate.assert_not_called()
        save.assert_not_called()
        session.append.assert_not_called()
        event.assert_called_once_with('taobao_account_start_failed','预期账号「买家甲」，实际为「买家乙」，采集未开始')

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
