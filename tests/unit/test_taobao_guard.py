"""淘宝守卫协议、控制、配置和任务状态测试，不读取真实账号。"""
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from ecommerce.cli.interaction import prompt_taobao_attention
from ecommerce.infrastructure.storage import database
from ecommerce.infrastructure.tasks.runner import current_task, run_task
from ecommerce.modules.account.taobao_control import request_control
from ecommerce.platforms.taobao.verification import Blockage, TaobaoGuard, retry_after_seconds, safe_location
from ecommerce.shared.config import load_settings
from ecommerce.shared.exceptions import NeedsAttention, TaskStopped


class TaobaoGuardTests(unittest.TestCase):
    def test_halted_state_has_no_continue_prompt_and_keeps_sanitized_reason(self):
        """业务退出发布保留现场标记，清除旧提示，网页不能把它当成可继续的暂停。"""
        with self.guard() as guard,patch('builtins.print'):
            guard._prompt_id='b'*32;guard._prompt_allowed={'继续'}
            self.page.is_closed.return_value=True
            guard.hold_without_input(reason='入库失败 password=PRIVATE')
            state=json.loads(guard.control_state_path.read_text(encoding='utf-8'))
            self.assertTrue(state['halted']);self.assertIsNone(state['prompt_id']);self.assertEqual(state['allowed'],[])
            self.assertNotIn('PRIVATE',state['message']);self.assertIn('入库失败',state['message'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = load_settings(root=Path(self.temp.name))
        self.context, self.page = MagicMock(), MagicMock()
        self.context.pages = [self.page]
        self.page.is_closed.return_value = False
        self.page.url = "https://www.taobao.com/?token=must-not-log#secret"

    def guard(self, callback=None):
        return TaobaoGuard(self.context, self.settings.log_dir, callback)

    def test_only_explicit_continue_can_release_a_disappeared_popup(self):
        event = Blockage("popup", self.page, "未知弹窗")
        calls = []
        def attention(message):
            calls.append(message)
            return "" if len(calls) == 1 else "继续"
        with self.guard(attention) as guard, patch.object(guard, "_scan", side_effect=[event, None, None]):
            action = MagicMock()
            guard.perform("读取商品", action)
            action.assert_called_once()
            self.assertEqual(len(calls), 2)
            self.assertEqual(guard.state, "running")
            records = guard.event_path.read_text(encoding="utf-8")
            self.assertNotIn("must-not-log", records)
            self.assertNotIn("#secret", records)

    def test_still_blocked_or_failed_resume_check_requires_more_feedback(self):
        event = Blockage("verification", self.page, "验证")
        attention = MagicMock(return_value="继续")
        with self.guard(attention) as guard, patch.object(guard, "_scan", side_effect=[event, event, None, None]):
            guard.resume_check = MagicMock(side_effect=[False, True])
            guard.checkpoint("翻页")
            self.assertEqual(attention.call_count, 3)
            self.assertTrue(attention.call_args_list[0].args[0].startswith("【需要人工验证】"))
            self.assertTrue(all(call.args[0].startswith("【验证尚未通过】") for call in attention.call_args_list[1:]))

    def test_missing_callback_and_stop_never_execute_action(self):
        for answer, expected in [(None, NeedsAttention), ("停止", TaskStopped)]:
            with self.subTest(answer=answer), self.guard(None if answer is None else lambda message: answer) as guard, \
                    patch.object(guard, "_scan", return_value=Blockage("popup", self.page, "未知弹窗")):
                action = MagicMock()
                with self.assertRaises(expected):
                    guard.perform("读取", action)
                action.assert_not_called()

    def test_business_checks_are_labeled_separately_and_still_require_explicit_feedback(self):
        messages = []
        def attention(message):
            messages.append(message)
            return '' if len(messages) == 1 else '继续'
        event = Blockage('business', self.page, '评价结构需要检查')
        with self.guard(attention) as guard, patch.object(guard, '_scan', side_effect=[event, None]):
            guard.checkpoint('检查评价结构')
        self.assertTrue(messages[0].startswith('【需要人工检查】'))
        self.assertTrue(messages[1].startswith('【检查尚未通过】'))
        self.assertEqual(guard.state, 'running')

    def test_cli_business_check_alert_sounds_once(self):
        with patch('builtins.input', return_value='继续'), patch('builtins.print'), \
                patch('ecommerce.cli.interaction._play_attention_sound') as sound:
            self.assertEqual(prompt_taobao_attention('【需要人工检查】'), '继续')
            self.assertEqual(prompt_taobao_attention('【检查尚未通过】'), '继续')
            sound.assert_called_once()

    def test_resume_business_failure_keeps_specific_reason_and_diagnostics(self):
        """主图恢复失败仍属于业务检查，不能改成登录提示，逐项差异保留在事件中。"""
        original=Blockage('business',self.page,'主图第1张来源或顺序不一致')
        current=Blockage('business',self.page,'主图第2张来源或顺序不一致',check_details={'position':2})
        messages=[]
        with self.guard(lambda message:messages.append(message) or '继续') as guard, \
                patch.object(guard,'_scan',side_effect=[original,None,None]):
            guard.resume_check=MagicMock(side_effect=[current,True])
            guard.checkpoint('复核主图列表')
            records=[json.loads(row) for row in guard.event_path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(messages),2);self.assertIn('第2张',messages[1])
        self.assertTrue(messages[1].startswith('【检查尚未通过】'));self.assertNotIn('登录状态',messages[1])
        self.assertTrue(any(row.get('check_details')=={'position':2} for row in records))

    def test_boolean_business_resume_failure_retains_original_reason(self):
        """兼容旧布尔恢复检查，业务失败仍保留原原因。"""
        messages=[];event=Blockage('business',self.page,'评价结构未确认')
        with self.guard(lambda message:messages.append(message) or '继续') as guard, \
                patch.object(guard,'_scan',side_effect=[event,None,None]):
            guard.resume_check=MagicMock(side_effect=[False,True]);guard.checkpoint('检查评价')
        self.assertIn('评价结构未确认',messages[1]);self.assertNotIn('登录状态',messages[1])

    def test_native_decision_does_not_accept_generic_continue(self):
        replies = iter(["继续", "取消"])
        dialog = MagicMock(type="confirm", message="是否执行？")
        with self.guard(lambda message: next(replies)) as guard:
            guard._native(self.page, dialog)
            dialog.accept.assert_not_called()
            dialog.dismiss.assert_called_once()
            self.assertEqual(guard._pending[0].kind, "native_done")

    def test_operation_error_is_not_replayed_and_needs_completion_evidence(self):
        from playwright.sync_api import Error
        with self.guard(lambda message: "继续") as guard, patch.object(guard, "_scan", return_value=None):
            action = MagicMock(side_effect=Error("navigation timeout"))
            self.assertIsNone(guard.perform("导航", action, completed=lambda: True))
            action.assert_called_once()

    def test_retry_after_and_url_redaction(self):
        self.assertEqual(retry_after_seconds("30"), 30)
        at = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("Tue, 20 Oct 2026 00:01:00 GMT", at=at), 60)
        for value in (None, "", "-1", "nan", "wrong"):
            self.assertIsNone(retry_after_seconds(value))
        self.assertEqual(safe_location("https://user:password@www.taobao.com/a?token=secret#fragment"), "https://www.taobao.com/a")

    def test_halted_session_records_real_reason_and_needs_attention(self):
        """非输入异常保留真实原因和暂停状态，不误报输入不可用或恢复业务。"""
        reason='本地事件确认文件无法核对'
        with self.guard() as guard, patch('builtins.print') as terminal:
            self.page.is_closed.return_value=True
            guard.hold_without_input(reason=reason)
            self.assertEqual(guard.state,'needs_attention')
            records=[json.loads(r) for r in guard.event_path.read_text(encoding='utf-8').splitlines()]
        halted=next(r for r in records if r['event']=='session_halted')
        self.assertEqual(halted['reason'],reason)
        self.assertEqual(halted['state'],'needs_attention')
        self.assertFalse(any(r['event']=='input_unavailable' for r in records))
        self.assertIn(reason,terminal.call_args.args[0])

    def test_halted_session_records_real_reason_and_needs_attention(self):
        """非输入异常保留真实原因和暂停状态，不误报输入不可用或恢复业务。"""
        reason='本地事件确认文件无法核对'
        with self.guard() as guard, patch('builtins.print') as terminal:
            self.page.is_closed.return_value=True
            guard.hold_without_input(reason=reason)
            self.assertEqual(guard.state,'needs_attention')
            records=[json.loads(r) for r in guard.event_path.read_text(encoding='utf-8').splitlines()]
        halted=next(r for r in records if r['event']=='session_halted')
        self.assertEqual(halted['reason'],reason)
        self.assertEqual(halted['state'],'needs_attention')
        self.assertFalse(any(r['event']=='input_unavailable' for r in records))
        self.assertIn(reason,terminal.call_args.args[0])

    def test_only_relevant_failures_trigger_network_pause(self):
        with self.guard() as guard:
            response = MagicMock(status=429, url="https://www.taobao.com/list?token=secret")
            response.headers = {"retry-after": "10"}
            request = response.request
            request.url = response.url
            request.is_navigation_request.return_value = True
            request.frame.page.main_frame = request.frame
            guard._response(response)
            self.assertEqual(len(guard._pending), 1)
            self.assertNotIn("secret", guard._pending[0].source)
            response.url = request.url = "https://ads.example/track"
            guard._response(response)
            self.assertEqual(len(guard._pending), 1)
            response.url = request.url = "https://www.taobao.com/image.png"
            request.is_navigation_request.return_value = False
            guard._response(response)
            self.assertEqual(len(guard._pending), 1)

    def test_task_progress_remains_unfinished_until_guard_returns(self):
        def execute():
            with self.guard() as guard:
                self.assertEqual(current_task()[1], guard.task_id)
                guard._transition("needs_attention")
                task = database.list_tasks(self.settings.database)[0]
                self.assertEqual(task["status"], "needs_attention")
                self.assertIsNone(task["finished_at"])
                guard._transition("running")
            return "done"
        result = run_task(self.settings, "account.taobao.check", execute)
        self.assertEqual(result.status, "succeeded")
        self.assertIsNone(current_task())

    def test_pause_and_stop_controls_target_current_task(self):
        def execute():
            with self.guard(lambda message: "继续") as guard:
                self.assertIn("已发送暂停", request_control(self.settings, "pause"))
                guard.checkpoint("保留窗口")
                self.assertFalse(guard.control_path.exists())
                self.assertIn("已发送停止", request_control(self.settings, "stop"))
                guard.checkpoint()
            self.fail("停止后不能继续")
        # 检测真实 DOM 的部分由浏览器测试覆盖。
        with patch.object(TaobaoGuard, "_scan", autospec=True) as scan:
            def inspect(guard):
                guard._check_control(self.page)
                return guard._pending[0] if guard._pending else None
            scan.side_effect = inspect
            result = run_task(self.settings, "account.taobao.login", execute)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.exit_code, 130)
        self.assertIn("没有执行中的", request_control(self.settings, "stop"))

    def test_unfinished_attention_is_recovered_after_process_exit(self):
        database.initialize(self.settings.database)
        task_id = database.create_task(self.settings.database, "account.taobao.login")
        database.update_task_progress(self.settings.database, task_id, "needs_attention", "等待人工")
        run_task(self.settings, "next", lambda: "ok")
        previous = next(task for task in database.list_tasks(self.settings.database) if task["id"] == task_id)
        self.assertIsNotNone(previous["finished_at"])

    def test_pacing_configuration_rejects_invalid_ranges(self):
        path = self.settings.root / "settings.toml"
        path.write_text("[taobao.pacing]\nscroll = [1.0, 2.5]")
        self.assertEqual(load_settings(str(path), root=self.settings.root).taobao_timing["scroll"], (1, 2.5))
        for value in ("[-1, 2]", "[3, 2]", "[true, 2]", "[nan, 3]", "[1]"):
            with self.subTest(value=value):
                path.write_text("[taobao.pacing]\nscroll=" + value)
                with self.assertRaises(ValueError):
                    load_settings(str(path), root=self.settings.root)

    def test_cli_rejects_empty_feedback_and_supports_stop(self):
        with patch("builtins.input", side_effect=["", "已完成", "继续"]), patch("builtins.print"), \
                patch("ecommerce.cli.interaction._play_attention_sound") as sound:
            self.assertEqual(prompt_taobao_attention("【需要人工验证】"), "继续")
            sound.assert_called_once()
        with patch("builtins.input", return_value="停止"), patch("builtins.print"):
            with self.assertRaises(TaskStopped):
                prompt_taobao_attention("【验证尚未通过】")

    def test_window_close_and_external_stop_are_detected_while_reading_feedback(self):
        with self.guard() as guard:
            guard._feedback_thread = object()  # 反馈队列保持空，模拟用户尚未输入。
            self.page.wait_for_timeout.side_effect = lambda ms: setattr(self.context, "pages", [])
            with self.assertRaises(TaskStopped):
                guard.read_feedback("等待继续")
        self.context.pages = [self.page]
        with self.guard() as guard:
            guard._feedback_thread = object()
            guard.control_path.parent.mkdir(parents=True, exist_ok=True)
            guard.control_path.write_text('{"action":"stop"}')
            with self.assertRaises(TaskStopped):
                guard.read_feedback("等待继续")

    def test_closed_feedback_stream_never_means_continue(self):
        with self.guard() as guard:
            guard._feedback_thread = object()
            guard._feedback_queue.put(None)
            def stop_after_eof(ms):
                self.assertTrue(guard._stdin_unavailable)
                guard.control_path.write_text('{"action":"stop"}')
            self.page.wait_for_timeout.side_effect = stop_after_eof
            with self.assertRaises(TaskStopped):
                guard.read_feedback("等待继续")

    def test_external_feedback_is_bound_to_current_prompt(self):
        def execute():
            with self.guard() as guard:
                guard._feedback_thread = object()
                def send(ms):
                    self.assertIn("已发送继续", request_control(self.settings, "continue"))
                self.page.wait_for_timeout.side_effect = send
                self.assertEqual(guard.read_feedback("等待继续"), "继续")
                with self.assertRaises(ValueError):
                    request_control(self.settings, "continue")
            return "ok"
        self.assertEqual(run_task(self.settings, "collection.taobao.search", execute).status, "succeeded")

    def test_delayed_control_feedback_cannot_release_new_prompt(self):
        with self.guard() as guard:
            guard._feedback_thread = object()
            calls = []
            def send(ms):
                calls.append(ms)
                command = {"action": "continue", "prompt_id": "expired-prompt"} if len(calls) == 1 else {"action": "stop"}
                guard.control_path.write_text(json.dumps(command), encoding="utf-8")
            self.page.wait_for_timeout.side_effect = send
            with self.assertRaises(TaskStopped):
                guard.read_feedback("等待继续")
            self.assertIn("stale_feedback_ignored", guard.event_path.read_text(encoding="utf-8"))

    def test_old_feedback_cannot_release_a_later_prompt(self):
        with self.guard() as guard:
            guard._feedback_thread = object()
            guard._feedback_queue.put((time.monotonic() - 10, "继续"))
            self.page.wait_for_timeout.side_effect = lambda ms: guard._feedback_queue.put((time.monotonic(), "停止"))
            self.assertEqual(guard.read_feedback("等待继续"), "停止")


if __name__ == "__main__":
    unittest.main()
