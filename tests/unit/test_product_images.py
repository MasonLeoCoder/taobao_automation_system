"""主图输入检查和业务接入回归测试。"""
import tempfile
import unittest
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from ecommerce.modules.product.assets import load_main_images, load_portrait_images
from ecommerce.modules.product import service
from ecommerce.shared.config import load_settings


class ProductImageTests(unittest.TestCase):
    """使用临时文件与模拟页面，不操作真实店铺。"""

    def setUp(self):
        """建立编号图片样例；参数：无；返回：无。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "images"
        self.folder.mkdir()
        for name in ("5.jpg", "2.png", "4.jpg", "1.png", "3.jpg"):
            (self.folder / name).write_bytes(b"test")
        self.settings = load_settings(root=self.root)

    def test_image_order_and_invalid_inputs(self):
        """编号必须完整唯一，空文件不能上传；参数：无；返回：无。"""
        self.assertEqual([p.name for p in load_main_images(self.folder)],
                         ["1.png", "2.png", "3.jpg", "4.jpg", "5.jpg"])
        (self.folder / "1.jpg").write_bytes(b"duplicate")
        with self.assertRaises(ValueError):
            load_main_images(self.folder)
        (self.folder / "1.jpg").unlink()
        (self.folder / "3.jpg").write_bytes(b"")
        with self.assertRaises(ValueError):
            load_main_images(self.folder)
        (self.folder / "3.jpg").unlink()
        with self.assertRaises(ValueError):
            load_main_images(self.folder)

    def test_invalid_directory_never_opens_browser(self):
        """文件错误在浏览器启动前报告；参数：无；返回：无。"""
        with patch.object(service, "browser_session") as browser:
            with self.assertRaises(ValueError):
                service.open_publish(self.settings, keyword="测试", category="一级 > 二级",
                                     brand="测试品牌", main_images_dir="missing")
            browser.assert_not_called()

    def test_portrait_order_with_unnumbered_fifth_image(self):
        """1～4按编号排列，唯一未编号图片排最后；参数：无；返回：无。"""
        (self.folder / "5.jpg").rename(self.folder / "家居展示图-3.jpg")
        self.assertEqual([p.name for p in load_portrait_images(self.folder)],
                         ["1.png", "2.png", "3.jpg", "4.jpg", "家居展示图-3.jpg"])
        (self.folder / "4.jpg").rename(self.folder / "另一个未编号.jpg")
        with self.assertRaises(ValueError):
            load_portrait_images(self.folder)

    def test_portrait_upload_routes_to_correct_aspect_ratio(self):
        """3:4上传使用独立比例参数；参数：无；返回：无。"""
        from ecommerce.platforms.qianniu import product_images
        page = MagicMock()
        files = load_portrait_images(self.folder)
        with patch.object(product_images, "_upload_main_images") as upload:
            product_images.upload_portrait_images(page, files)
        upload.assert_called_once_with(page, files, "3:4")

    def test_manual_verification_waits_for_user_and_checks_again(self):
        """验证出现时调用人工回调，处理后再次确认；参数：无；返回：无。"""
        from ecommerce.platforms.qianniu import verification as product_images
        page, callback = MagicMock(), MagicMock()
        with patch.object(product_images, "has_human_verification", side_effect=[True, False]):
            product_images.wait_for_human_verification(page, callback)
        callback.assert_called_once()
        page.mouse.move.assert_not_called()
        page.mouse.down.assert_not_called()

    def test_verification_without_callback_needs_attention(self):
        """无交互入口时明确转为人工处理；参数：无；返回：无。"""
        from ecommerce.platforms.qianniu import verification as product_images
        from ecommerce.shared.exceptions import NeedsAttention
        with patch.object(product_images, "has_human_verification", return_value=True):
            with self.assertRaises(NeedsAttention):
                product_images.wait_for_human_verification(MagicMock(), None)

    def test_unresolved_verification_does_not_resume(self):
        """多次反馈仍有验证时继续等待，不限制人工确认次数；参数：无；返回：无。"""
        from ecommerce.platforms.qianniu import verification as product_images
        callback = MagicMock()
        with patch.object(product_images, "has_human_verification", side_effect=[True, True, True, True, True, False]):
            product_images.wait_for_human_verification(MagicMock(), callback, step="选择第2张3:4主图")
        self.assertEqual(callback.call_count, 5)
        self.assertIn("选择第2张3:4主图", callback.call_args_list[0].args[0])
        self.assertTrue(callback.call_args_list[0].args[0].startswith("【需要人工验证】"))
        for call in callback.call_args_list[1:]:
            self.assertTrue(call.args[0].startswith("【验证尚未通过】"))

    def test_no_feedback_does_not_resume_even_if_verification_disappears(self):
        """页面验证消失也必须等到用户反馈；参数：无；返回：无。"""
        from ecommerce.platforms.qianniu import verification as product_images
        entered, feedback, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def callback(message):
            """模拟阻塞输入；参数：提示；返回：反馈到达后返回。"""
            entered.set()
            feedback.wait()

        def execute_wait():
            """执行等待并记录结果；参数：无；返回：无。"""
            try:
                product_images.wait_for_human_verification(MagicMock(), callback)
                finished.set()
            except Exception as error:
                errors.append(error)

        with patch.object(product_images, "has_human_verification", side_effect=[True, False]):
            worker = threading.Thread(target=execute_wait, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(finished.is_set())
            finally:
                feedback.set()
                worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(finished.is_set())

    def test_cli_prompt_beeps_once_and_requires_explicit_confirmation(self):
        """一次提示音，空回车或等待回复不能恢复；参数：无；返回：无。"""
        from ecommerce.cli.commands.product import prompt_manual_verification
        sound = MagicMock()
        with patch.dict(sys.modules, {"winsound": sound}), patch("builtins.print"), patch("builtins.input", side_effect=["", "等等", "继续"]) as response:
            prompt_manual_verification("【需要人工验证】\n当前步骤：测试主图")
        sound.MessageBeep.assert_called_once()
        self.assertEqual(response.call_count, 3)
        sound.reset_mock()
        with patch.dict(sys.modules, {"winsound": sound}), patch("builtins.print"), patch("builtins.input", return_value="已完成"):
            prompt_manual_verification("【验证尚未通过】\n当前步骤：测试主图")
        sound.MessageBeep.assert_not_called()

    def test_both_image_groups_complete_before_ready(self):
        """先填1:1再填3:4，完成后才就绪；参数：无；返回：无。"""
        calls = []
        with ExitStack() as stack:
            browser = stack.enter_context(patch.object(service, "browser_session"))
            page = MagicMock()
            browser.return_value.__enter__.return_value = (MagicMock(), page)
            for name in ("authenticate", "click_product_nav", "click_publish_product", "select_brand_and_continue"):
                stack.enter_context(patch.object(service, name, return_value=page))
            for name in ("search_publish_products", "select_publish_category"):
                stack.enter_context(patch.object(service, name))
            stack.enter_context(patch.object(service, "upload_main_images", side_effect=lambda *args: calls.append("1:1")))
            portrait = stack.enter_context(patch.object(service, "upload_portrait_images", side_effect=lambda *args: calls.append("3:4")))
            service.open_publish(self.settings, on_ready=lambda: calls.append("ready"),
                                 keyword="测试", category="一级 > 二级", brand="测试品牌",
                                 main_images_dir="images", portrait_images_dir="images")
            portrait.assert_called_once_with(page, load_portrait_images(self.folder))
        self.assertEqual(calls, ["1:1", "3:4", "ready"])

    def test_file_chooser_interception_covers_upload_and_is_released(self):
        """额外文件请求保持拦截，失败后也释放监听；参数：无；返回：无。"""
        from ecommerce.platforms.qianniu import product_images
        for fail in (False, True):
            with self.subTest(fail=fail):
                page = MagicMock()

                def perform_upload(current_page, files):
                    """核对上传期间监听有效；参数：页面、图片；返回：无。"""
                    page.on.assert_called_once()
                    page.remove_listener.assert_not_called()
                    if fail:
                        raise RuntimeError("上传失败")

                with patch.object(product_images, "_upload_main_images", side_effect=perform_upload):
                    if fail:
                        with self.assertRaises(RuntimeError):
                            product_images.upload_main_images(page, [])
                    else:
                        product_images.upload_main_images(page, [])
                event, listener = page.on.call_args.args
                self.assertEqual(event, "filechooser")
                page.remove_listener.assert_called_once_with(event, listener)

    def test_upload_uses_editor_page_and_failure_does_not_report_ready(self):
        """进入编辑页后才上传，上传失败不能执行就绪回调；参数：无；返回：无。"""
        for fail in (False, True):
            with self.subTest(fail=fail), ExitStack() as stack:
                browser = stack.enter_context(patch.object(service, "browser_session"))
                page, editor = MagicMock(), MagicMock()
                browser.return_value.__enter__.return_value = (MagicMock(), page)
                for name in ("authenticate", "click_product_nav", "click_publish_product"):
                    stack.enter_context(patch.object(service, name, return_value=page))
                stack.enter_context(patch.object(service, "search_publish_products"))
                stack.enter_context(patch.object(service, "select_publish_category"))
                stack.enter_context(patch.object(service, "select_brand_and_continue", return_value=editor))
                upload = stack.enter_context(patch.object(service, "upload_main_images"))
                ready = MagicMock()
                if fail:
                    upload.side_effect = RuntimeError("上传失败")
                    with self.assertRaisesRegex(RuntimeError, "上传失败"):
                        service.open_publish(self.settings, on_ready=ready, keyword="测试",
                                             category="一级 > 二级", brand="测试品牌", main_images_dir="images")
                    ready.assert_not_called()
                else:
                    service.open_publish(self.settings, on_ready=ready, keyword="测试",
                                         category="一级 > 二级", brand="测试品牌", main_images_dir="images")
                    ready.assert_called_once()
                upload.assert_called_once_with(editor, load_main_images(self.folder))
