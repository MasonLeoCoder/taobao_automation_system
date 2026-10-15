"""人工弹窗入口与现场记录失败的回归。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from ecommerce.cli import interaction
from ecommerce.platforms.qianniu.popups import Popup, PopupGuard
from ecommerce.shared.exceptions import NeedsAttention


class PopupInteractionTests(unittest.TestCase):
    def test_native_requires_confirmation_or_cancellation(self):
        for decision in ['确认', '取消']:
            with self.subTest(decision=decision), patch.object(interaction, '_play_attention_sound') as sound, patch('builtins.print'), patch('builtins.input', side_effect=['', '继续', decision]) as response:
                self.assertEqual(interaction.prompt_manual_verification('【需要人工决定原生弹窗】'), decision)
                self.assertEqual(response.call_count, 3)
                sound.assert_called_once()

    def test_unknown_popup_waits_for_feedback_and_beeps_only_first_notice(self):
        with patch.object(interaction, '_play_attention_sound') as sound, patch('builtins.print'), patch('builtins.input', side_effect=['', '稍等', '继续', '继续']) as response:
            self.assertEqual(interaction.prompt_manual_verification('【需要人工处理弹窗】'), '继续')
            self.assertEqual(interaction.prompt_manual_verification('【弹窗尚未处理】'), '继续')
            self.assertEqual(response.call_count, 4)
            sound.assert_called_once()

    def test_closed_input_does_not_count_as_feedback(self):
        with patch.object(interaction, '_play_attention_sound'), patch('builtins.print'), patch('builtins.input', side_effect=EOFError):
            with self.assertRaisesRegex(NeedsAttention, '未收到人工处理反馈'):
                interaction.prompt_manual_verification('【需要人工处理弹窗】')

    def test_failed_record_does_not_disable_manual_wait(self):
        context, page = MagicMock(), MagicMock()
        page.url = 'https://myseller.taobao.com/home?token=secret'
        popup = Popup(page, None, 'unknown', {'title': '未知公告'})
        callback = MagicMock()
        with tempfile.TemporaryDirectory() as folder:
            guard = PopupGuard(context, callback, Path(folder))
            with patch.object(guard, '_scan', side_effect=[[popup], []]), patch.object(Path, 'mkdir', side_effect=PermissionError('只读目录')), self.assertLogs('ecommerce.platforms.qianniu.popups', level='ERROR'):
                guard.checkpoint('product.open', '测试')
        callback.assert_called_once()
        self.assertIn('现场记录保存失败', callback.call_args.args[0])
        self.assertNotIn('secret', callback.call_args.args[0])
