"""提交的错误日志、人工反馈及禁止自动重放测试。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from ecommerce.modules.product import service
from ecommerce.platforms.qianniu.product_submit import SubmissionResult
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention
from ecommerce.cli import interaction


class ProductSubmitTests(unittest.TestCase):
    def test_failure_waits_for_review_and_only_rechecks_result(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(root=Path(directory))
            page = MagicMock()
            page.context.pages = [page]
            review = MagicMock()
            failed = SubmissionResult("failed", "宝贝标题：必填项不能为空", page)
            success = SubmissionResult("succeeded", "宝贝发布成功", page)
            with patch.object(service, "submit_product_to_warehouse", return_value=failed) as submitter, patch.object(service, "inspect_submission_result", return_value=success) as inspector, self.assertLogs(service.__name__, level="ERROR") as logs:
                result = service.submit_with_review(settings, page, review)
            submitter.assert_called_once_with(page)
            inspector.assert_called_once_with(page, (page,))
            review.assert_called_once()
            self.assertIn("宝贝标题", review.call_args.args[0])
            self.assertIn("宝贝标题", logs.output[0])
            self.assertEqual(result, "宝贝发布成功")

    def test_unknown_result_without_interaction_is_needs_attention(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(root=Path(directory))
            page = MagicMock()
            page.context.pages = [page]
            with patch.object(service, "submit_product_to_warehouse", return_value=SubmissionResult("unknown", "未确认", page)), patch.object(service, "inspect_submission_result") as inspector, self.assertLogs(service.__name__, level="ERROR"):
                with self.assertRaisesRegex(NeedsAttention, "提交结果未确认"):
                    service.submit_with_review(settings, page)
            inspector.assert_not_called()

    def test_preflight_error_without_feedback_never_attempts_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(root=Path(directory))
            page = MagicMock()
            page.context.pages = [page]
            blocked = SubmissionResult("failed", "宝贝标题不能为空", page, submit_attempted=False)
            with patch.object(service, "submit_product_to_warehouse", return_value=blocked) as submitter, patch.object(service, "inspect_submission_result") as inspector, self.assertLogs(service.__name__, level="ERROR"):
                with self.assertRaisesRegex(NeedsAttention, "尚未点击提交"):
                    service.submit_with_review(settings, page)
            submitter.assert_called_once()
            inspector.assert_not_called()

    def test_preflight_error_rechecks_only_after_explicit_review(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(root=Path(directory))
            page = MagicMock()
            page.context.pages = [page]
            blocked = SubmissionResult("failed", "宝贝标题不能为空", page, submit_attempted=False)
            success = SubmissionResult("succeeded", "商品提交成功", page)
            with patch.object(service, "submit_product_to_warehouse", side_effect=[blocked, success]) as submitter, patch.object(service, "inspect_submission_result") as inspector, self.assertLogs(service.__name__, level="ERROR"):
                def review(message):
                    self.assertEqual(submitter.call_count, 1)
                    self.assertIn("未收到反馈前不会点击提交", message)
                self.assertEqual(service.submit_with_review(settings, page, review), "商品提交成功")
            self.assertEqual(submitter.call_count, 2)
            inspector.assert_not_called()

    def test_success_with_platform_issues_alerts_without_resubmitting(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(root=Path(directory))
            page = MagicMock()
            page.context.pages = [page]
            review = MagicMock()
            success = SubmissionResult("succeeded", "商品提交成功", page,
                                       ("商品多件优惠创建失败，商品不存在", "导购标题存在异常（流量受限）"), "123456")
            with patch.object(service, "submit_product_to_warehouse", return_value=success) as submitter, patch.object(service, "inspect_submission_result", return_value=success) as inspector, self.assertLogs(service.__name__, level="INFO") as logs:
                result = service.submit_with_review(settings, page, review)
            submitter.assert_called_once_with(page)
            inspector.assert_called_once_with(page, (page,))
            review.assert_called_once()
            self.assertIn("无需再次提交", review.call_args.args[0])
            self.assertIn("123456", result)
            self.assertIn("平台仍提示", result)
            self.assertTrue(any("ERROR" in entry and "创建失败" in entry for entry in logs.output))

    def test_existing_success_review_never_calls_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(root=Path(directory))
            page = MagicMock()
            page.context.pages = [page]
            result = SubmissionResult("succeeded", "商品提交成功", page, ("创建失败",), "123456")
            cleared = SubmissionResult("succeeded", "商品提交成功", page, item_id="123456")
            with patch.object(service, "submit_product_to_warehouse") as submitter, patch.object(service, "inspect_submission_result", return_value=cleared), self.assertLogs(service.__name__, level="ERROR"):
                message = service.review_submission_result(settings, page, result, MagicMock())
            submitter.assert_not_called()
            self.assertEqual(message, "商品提交成功；商品ID：123456")

    def test_success_with_issues_without_interaction_requires_attention(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(root=Path(directory))
            page = MagicMock()
            result = SubmissionResult("succeeded", "商品提交成功", page, ("创建失败",), "123456")
            with patch.object(service, "inspect_submission_result") as inspector, self.assertLogs(service.__name__, level="ERROR"):
                with self.assertRaisesRegex(NeedsAttention, "商品已提交，但平台提示异常"):
                    service.review_submission_result(settings, page, result)
            inspector.assert_not_called()

    def test_review_sound_and_explicit_feedback(self):
        with patch.object(interaction, "_play_attention_sound") as sound, patch("builtins.print"), patch("builtins.input", side_effect=["", "稍等", "继续"]) as response:
            interaction.prompt_submission_review("提交失败")
        sound.assert_called_once()
        self.assertEqual(response.call_count, 3)

    def test_closed_input_does_not_resume(self):
        with patch.object(interaction, "_play_attention_sound"), patch("builtins.print"), patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(NeedsAttention):
                interaction.prompt_submission_review("提交失败")

    def test_submit_cannot_skip_required_data_and_warehouse_selection(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(service, "browser_session") as browser:
            with self.assertRaises(ValueError):
                service.open_publish(load_settings(root=Path(directory)), submit=True)
            browser.assert_not_called()
