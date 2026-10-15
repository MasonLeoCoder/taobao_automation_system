"""全局验证守卫的失败、跨页和扩展处理器回归测试。"""
import unittest
from unittest.mock import MagicMock, patch
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from ecommerce.platforms.qianniu import verification as v
from ecommerce.shared.exceptions import NeedsAttention


class VerificationTests(unittest.TestCase):
    def page(self):
        page = MagicMock()
        page.is_closed.return_value = False
        return page

    def test_existing_and_new_pages_attach_and_cleanup(self):
        context = MagicMock()
        first, popup = self.page(), self.page()
        context.pages = [first]
        with v.VerificationGuard(context):
            event, listener = context.on.call_args.args
            self.assertEqual(event, "page")
            listener(popup)
            first.add_locator_handler.assert_called_once()
            popup.add_locator_handler.assert_called_once()
        context.remove_listener.assert_called_once_with("page", listener)
        first.remove_locator_handler.assert_called_once()
        popup.remove_locator_handler.assert_called_once()
        self.assertIsNone(v._ACTIVE.get())

    def test_verification_in_other_tab_blocks_operation(self):
        context = MagicMock()
        first, popup = self.page(), self.page()
        context.pages = [first, popup]
        pending = [True]
        action = MagicMock()
        def feedback(message):
            action.assert_not_called()
            self.assertIn("需要人工验证", message)
            pending[0] = False
        with v.VerificationGuard(context, feedback), patch.object(v, "has_human_verification", side_effect=lambda p: p is popup and pending[0]):
            v.qianniu_operation(action)()
        action.assert_called_once()

    def test_custom_handler_return_is_rechecked_and_falls_back(self):
        context = MagicMock()
        context.pages = [self.page()]
        custom, manual = MagicMock(), MagicMock()
        with v.VerificationGuard(context, manual, handler=custom) as guard, patch.object(v, "has_human_verification", side_effect=[True, True, False]):
            guard.checkpoint("填写品牌")
        custom.assert_called_once()
        manual.assert_called_once()
        request = custom.call_args.args[0]
        self.assertEqual(request.step, "填写品牌")
        self.assertIs(request.page, context.pages[0])

    def test_timeout_after_handling_never_replays_operation(self):
        context = MagicMock()
        context.pages = [self.page()]
        calls = []
        @v.qianniu_operation
        def submit():
            """提交商品。"""
            calls.append("submitted")
            raise PlaywrightTimeoutError("timeout")
        with v.VerificationGuard(context, MagicMock()), patch.object(v, "has_human_verification", side_effect=[False, True, False]):
            with self.assertRaisesRegex(NeedsAttention, "结果尚未确认"):
                submit()
        self.assertEqual(calls, ["submitted"])

    def test_regular_timeout_is_not_misreported_as_verification(self):
        context = MagicMock()
        context.pages = [self.page()]
        @v.qianniu_operation
        def action():
            raise PlaywrightTimeoutError("missing selector")
        with v.VerificationGuard(context), patch.object(v, "has_human_verification", return_value=False):
            with self.assertRaisesRegex(PlaywrightTimeoutError, "missing selector"):
                action()

    def test_closed_browser_and_missing_callback_need_attention(self):
        context = MagicMock()
        context.pages = []
        with v.VerificationGuard(context) as guard:
            with self.assertRaisesRegex(NeedsAttention, "浏览器已关闭"):
                guard.checkpoint()
        context.pages = [self.page()]
        with v.VerificationGuard(context) as guard, patch.object(v, "has_human_verification", return_value=True):
            with self.assertRaisesRegex(NeedsAttention, "需要人工验证"):
                guard.checkpoint()

    def test_cli_eof_does_not_count_as_feedback(self):
        from ecommerce.cli.interaction import prompt_manual_verification
        with patch("builtins.input", side_effect=EOFError), patch("builtins.print"):
            with self.assertRaises(NeedsAttention):
                prompt_manual_verification("【验证尚未通过】")

    def test_file_dialog_scope_never_uploads_and_always_releases_its_own_listener(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                page, chooser = MagicMock(), MagicMock()
                def run_scope():
                    with v.intercept_file_dialogs(page):
                        event, listener = page.on.call_args.args
                        self.assertEqual(event, 'filechooser')
                        listener(chooser)
                        chooser.set_files.assert_not_called()
                        page.remove_listener.assert_not_called()
                        if fail:
                            raise RuntimeError('上传中断')
                if fail:
                    with self.assertRaisesRegex(RuntimeError, '上传中断'):
                        run_scope()
                else:
                    run_scope()
                event, listener = page.on.call_args.args
                page.remove_listener.assert_called_once_with(event, listener)

    def test_sku_entry_keeps_file_dialog_scope_until_entire_operation_finishes(self):
        from ecommerce.platforms.qianniu import product_sku_media as sku
        for fail in (False, True):
            with self.subTest(fail=fail):
                page = MagicMock()
                expected = sku.SkuMediaResult({'规格': ['SKU搜索标题']}, {})
                def fill(*args):
                    page.on.assert_called_once()
                    page.remove_listener.assert_not_called()
                    if fail:
                        raise RuntimeError('保存中断')
                    return expected
                with patch.object(sku, '_fill_sku_media', side_effect=fill):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, '保存中断'):
                            sku.fill_sku_media(page, ['规格'], MagicMock(), '导购标题')
                    else:
                        self.assertIs(sku.fill_sku_media(page, ['规格'], MagicMock(), '导购标题'), expected)
                event, listener = page.on.call_args.args
                page.remove_listener.assert_called_once_with(event, listener)
