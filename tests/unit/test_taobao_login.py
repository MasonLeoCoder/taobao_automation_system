"""验证淘宝登录隔离、成功证据及失败时不保存状态。"""
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from ecommerce.cli.app import parser
from ecommerce.cli.commands import account
from ecommerce.cli.commands import collection
from ecommerce.modules.account import taobao_service
from ecommerce.platforms.taobao import login
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention


def page_with(url=login.HOME_URL, nickname="", visible=True, anonymous=False):
    page = MagicMock()
    page.url = url
    page.is_closed.return_value = False
    page.context.pages = [page]

    def locate(selector):
        result = MagicMock()
        element = MagicMock()
        element.is_visible.return_value = visible
        element.inner_text.return_value = "亲，请登录" if selector == login.ANONYMOUS_SELECTOR else nickname
        element.get_attribute.return_value = login.LOGIN_URL
        result.all.return_value = [element] if (anonymous if selector == login.ANONYMOUS_SELECTOR else True) else []
        return result

    page.locator.side_effect = locate
    return page


class TaobaoLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = load_settings(root=self.root)

    def test_separate_paths_even_with_legacy_qianniu_state(self):
        (self.root / "state.json").write_text("{}")
        (self.root / "storage/taobao_browser_profile").mkdir(parents=True)
        settings = load_settings(root=self.root)
        taobao = settings.for_platform("taobao")
        self.assertEqual(settings.session_path, self.root / "state.json")
        self.assertEqual(taobao.session_path, self.root / "workspace/storage/sessions/taobao.json")
        self.assertEqual(taobao.profile_path, self.root / "workspace/storage/browser_profiles/taobao")
        self.assertNotEqual(taobao.profile_path, settings.profile_path)
        self.assertEqual(taobao.database, settings.database)

    def test_custom_paths_and_conflicting_configuration(self):
        config = self.root / "custom.toml"
        config.write_text('[taobao]\nsession_path="custom/taobao.json"\nprofile_path="custom/taobao"')
        settings = load_settings(str(config), root=self.root)
        self.assertEqual(settings.for_platform("taobao").session_path, self.root / "custom/taobao.json")
        for fields in ('session_path="workspace/storage/sessions/qianniu.json"',
                       'profile_path="workspace/storage/browser_profiles/qianniu"',
                       'profile_path="workspace/storage/browser_profiles/qianniu/child"',
                       'session_path="workspace/storage/browser_profiles/qianniu/state.json"'):
            with self.subTest(fields=fields):
                config.write_text("[taobao]\n" + fields)
                with self.assertRaises(ValueError):
                    load_settings(str(config), root=self.root)

    def test_cli_routes_taobao_without_changing_qianniu_default(self):
        self.assertEqual(parser().parse_args(["account", "login"]).platform, "qianniu")
        self.assertEqual(parser().parse_args(["account", "login", "--platform", "taobao"]).platform, "taobao")
        with patch.object(account, "run_task", side_effect=lambda settings, name, action: action()) as run, \
                patch.object(taobao_service, "login", return_value="ok") as handler:
            self.assertEqual(account.execute(self.settings, "login", "taobao"), "ok")
            self.assertEqual(run.call_args.args[1], "account.taobao.login")
            self.assertEqual(handler.call_args.args[0].profile_path, self.settings.for_platform("taobao").profile_path)

    def test_managed_cli_requires_explicit_buyer_in_production_and_test(self):
        for environment in ('production','test'):
            settings = replace(self.settings, task_backend='mysql', environment=environment)
            with self.subTest(environment=environment), patch.object(account,'run_task') as run:
                with self.assertRaisesRegex(ValueError,'--account'):
                    account.execute(settings,'check','taobao')
                with self.assertRaisesRegex(ValueError,'--account'):
                    collection.execute(settings,'样例')
                run.assert_not_called()

    def test_managed_cli_inspects_only_requested_buyer_under_taobao_lock(self):
        settings = replace(self.settings, task_backend='mysql', environment='production')
        selected = {'id':'a'*32,'login_name':'样例买家','revision':3}
        result = parser().parse_args(['account','login','--platform','taobao','--account',selected['login_name'],'--no-wait'])
        with (patch('ecommerce.modules.account.buyers.named_account',return_value=selected) as lookup,
              patch('ecommerce.modules.account.web_buyers.BuyerAccountWorkbench') as workbench,
              patch.object(account,'run_task',side_effect=lambda actual,name,work,**options: work()) as run):
            account.execute(settings,result.action,result.platform,account=result.account,wait=not result.no_wait)
        self.assertEqual(lookup.call_args.args[1],selected['login_name'])
        self.assertEqual(run.call_args.args[0].profile_path,settings.for_platform('taobao').profile_path)
        self.assertEqual(run.call_args.kwargs['buyer_scope'],{'account_id':selected['id'],'login_name':selected['login_name']})
        operation = workbench.return_value.inspect_selected.call_args
        self.assertEqual(operation.args,('login',selected))
        self.assertFalse(operation.kwargs['wait'])

    def test_browser_channel_is_optional_and_validated(self):
        self.assertIsNone(self.settings.taobao_browser_channel)
        config = self.root / "channel.toml"
        for channel in ("chrome", "msedge"):
            config.write_text(f'[taobao]\nbrowser_channel="{channel}"', encoding="utf-8")
            selected = load_settings(str(config), root=self.root).for_platform("taobao")
            self.assertEqual(selected.taobao_browser_channel, channel)
            self.assertEqual(selected.profile_path, self.settings.for_platform("taobao").profile_path)
        for value in ('"unknown"', '""', '123', 'true', '[]'):
            config.write_text(f'[taobao]\nbrowser_channel={value}', encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_settings(str(config), root=self.root)

    def test_taobao_passes_system_browser_choice_without_restoring_old_export(self):
        from dataclasses import replace
        from ecommerce.platforms.taobao import session
        settings = replace(self.settings.for_platform("taobao"), taobao_browser_channel="chrome")
        (settings.profile_path / "Default").mkdir(parents=True)
        with patch.object(session, "base_browser_session") as browser, patch.object(session, "TaobaoGuard"):
            browser.return_value.__enter__.return_value = (MagicMock(), MagicMock())
            with session.browser_session(settings):
                pass
            browser.assert_called_once_with(settings, restore_state=False, channel="chrome")

    def test_generic_browser_uses_channel_only_when_explicitly_selected(self):
        from ecommerce.infrastructure.browser.session import browser_session
        for channel in (None, "chrome"):
            with self.subTest(channel=channel), patch("playwright.sync_api.sync_playwright") as factory:
                launch = factory.return_value.__enter__.return_value.chromium.launch_persistent_context
                with browser_session(self.settings, restore_state=False, channel=channel):
                    pass
                if channel is None:
                    self.assertNotIn("channel", launch.call_args.kwargs)
                else:
                    self.assertEqual(launch.call_args.kwargs["channel"], channel)

    def test_only_visible_nickname_on_taobao_home_confirms_login(self):
        self.assertTrue(login.is_authenticated(page_with(nickname="测试账号")))
        for page in (page_with(), page_with(nickname="测试账号", visible=False),
                     page_with(nickname="测试账号", anonymous=True),
                     page_with(url="https://myseller.taobao.com/home.htm", nickname="测试账号"),
                     page_with(url="https://www.taobao.com.evil.example/", nickname="测试账号"),
                     page_with(url=login.LOGIN_URL, nickname="测试账号")):
            self.assertFalse(login.is_authenticated(page))

    def test_manual_login_can_complete_in_new_tab(self):
        page, home = page_with(url=login.LOGIN_URL), page_with(nickname="测试账号")
        page.context.pages = [page, home]
        self.assertIs(login.login_manually(page, 1000), home)
        page.goto.assert_called_once_with(login.LOGIN_URL, wait_until="domcontentloaded", timeout=1000)
        page.reload.assert_not_called()

    def test_closed_browser_stops_wait_without_success(self):
        page = page_with(url=login.LOGIN_URL)
        page.context.pages = []
        with self.assertRaises(NeedsAttention):
            login.login_manually(page, 1000)

    def test_success_rechecks_home_before_saving(self):
        home, context = MagicMock(), MagicMock()
        with patch.object(taobao_service, "check_login", side_effect=[False, True]) as check, \
                patch.object(taobao_service, "login_manually", return_value=home), \
                patch.object(taobao_service, "save_session") as save:
            self.assertIs(taobao_service.authenticate(context, MagicMock(), self.settings), home)
            self.assertIs(check.call_args.args[0], home)
            save.assert_called_once_with(context, self.settings)

    def test_login_keeps_window_after_saving_until_user_closes(self):
        context, page = MagicMock(), MagicMock()
        events = []
        with patch.object(taobao_service, "browser_session") as session, \
                patch.object(taobao_service, "authenticate", side_effect=lambda *args: events.append("saved")), \
                patch.object(taobao_service, "wait_until_browser_closed", side_effect=lambda ctx: events.append("wait")) as wait:
            session.return_value.__enter__.return_value = (context, page)
            session.return_value.__exit__.side_effect = lambda *args: events.append("closed")
            self.assertIn("登录成功", taobao_service.login(self.settings))
            self.assertEqual(events, ["saved", "wait", "closed"])
            wait.assert_called_once_with(context)

    def test_explicit_no_wait_closes_after_saving(self):
        with patch.object(taobao_service, "browser_session") as session, \
                patch.object(taobao_service, "authenticate") as authenticate, \
                patch.object(taobao_service, "wait_until_browser_closed") as wait:
            session.return_value.__enter__.return_value = (MagicMock(), MagicMock())
            taobao_service.login(self.settings, wait=False)
            authenticate.assert_called_once()
            wait.assert_not_called()
        with patch.object(account, "run_task", side_effect=lambda settings, name, action: action()), \
                patch.object(taobao_service, "login", return_value="ok") as handler:
            account.execute(self.settings, "login", "taobao", wait=False)
            self.assertIs(handler.call_args.kwargs["wait"], False)

    def test_wait_continues_until_last_tab_is_closed(self):
        context, first, second = MagicMock(), MagicMock(), MagicMock()
        first.is_closed.return_value = second.is_closed.return_value = False
        type(context).pages = PropertyMock(side_effect=[[first, second], [first], []])
        login.wait_until_browser_closed(context)
        first.wait_for_timeout.assert_called_once()
        second.wait_for_timeout.assert_called_once()

    def test_last_window_closing_during_wait_is_normal(self):
        context, page = MagicMock(), MagicMock()
        page.is_closed.side_effect = [False, True]
        type(context).pages = PropertyMock(side_effect=[[page], []])
        page.wait_for_timeout.side_effect = login.PlaywrightError("Target page closed")
        login.wait_until_browser_closed(context)

    def test_unexpected_wait_error_is_not_ignored(self):
        context, page = MagicMock(), MagicMock()
        context.pages = [page]
        page.is_closed.return_value = False
        page.wait_for_timeout.side_effect = login.PlaywrightError("Browser connection error")
        with self.assertRaises(login.PlaywrightError):
            login.wait_until_browser_closed(context)

    def test_unconfirmed_login_check_and_timeouts_never_save(self):
        cases = [(False, False), (True, False)]
        for allow_login, authenticated in cases:
            with self.subTest(allow_login=allow_login), \
                    patch.object(taobao_service, "check_login", return_value=authenticated), \
                    patch.object(taobao_service, "login_manually"), \
                    patch.object(taobao_service, "save_session") as save:
                with self.assertRaises(NeedsAttention):
                    taobao_service.authenticate(MagicMock(), MagicMock(), self.settings, allow_login)
                save.assert_not_called()
        with patch.object(taobao_service, "check_login", side_effect=login.PlaywrightTimeoutError("timeout")), \
                patch.object(taobao_service, "save_session") as save:
            with self.assertRaises(NeedsAttention):
                taobao_service.authenticate(MagicMock(), MagicMock(), self.settings)
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
