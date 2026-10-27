"""淘宝会话中业务人工异常的窗口生命周期；不启动真实浏览器。"""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock,patch
import tempfile
import unittest
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention,TaskStopped
from ecommerce.platforms.taobao import session


class AttentionSessionTests(unittest.TestCase):
    def test_visible_business_attention_keeps_window_before_cleanup_even_without_guard_failure(self):
        """普通业务NeedsAttention同样先保留现场，不能因guard.failure为空而直接关窗。"""
        with tempfile.TemporaryDirectory() as folder:
            settings=load_settings(root=Path(folder)).for_platform('taobao')
            steps=[];context=MagicMock();page=MagicMock();guard=MagicMock(failure=None)
            @contextmanager
            def browser(*args,**kwargs):
                steps.append('opened')
                try:yield context,page
                finally:steps.append('closed')
            def hold(**kwargs):
                self.assertEqual(steps,['opened']);self.assertEqual(kwargs['reason'],'合成原件异常')
                steps.append('held')
            guard.hold_without_input.side_effect=hold
            factory=MagicMock();factory.return_value.__enter__.return_value=guard
            with patch.object(session,'base_browser_session',browser),patch.object(session,'TaobaoGuard',factory):
                with self.assertRaises(NeedsAttention),session.browser_session(settings):raise NeedsAttention('合成原件异常')
            self.assertEqual(steps,['opened','held','closed'])

    def test_headless_attention_and_explicit_stop_do_not_enter_hold(self):
        """无界面异常和明确停止不进入保留现场等待。"""
        with tempfile.TemporaryDirectory() as folder:
            for headless,error in ((True,NeedsAttention('合成异常')),(False,TaskStopped('合成停止'))):
                settings=replace(load_settings(root=Path(folder)).for_platform('taobao'),headless=headless)
                with patch.object(session,'base_browser_session') as browser,patch.object(session,'TaobaoGuard') as factory:
                    browser.return_value.__enter__.return_value=MagicMock(),MagicMock()
                    guard=factory.return_value.__enter__.return_value;guard.failure=None
                    with self.assertRaises(type(error)),session.browser_session(settings):raise error
                    guard.hold_without_input.assert_not_called()
