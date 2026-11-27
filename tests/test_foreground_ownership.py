from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from wechat_autoreply import wechat_ui as ui


class RunningWeChatPIDTests(unittest.TestCase):
    def test_returns_only_a_single_existing_process(self):
        with patch.object(ui, "run") as query:
            for value, expected in (("42\n", 42), ("", 0), ("42, 77", 0), ("-1", 0)):
                query.return_value.stdout = value
                self.assertEqual(ui._running_wechat_pid(), expected)
            self.assertIn("every application process whose bundle identifier", query.call_args.args[0][-1])
            query.side_effect = RuntimeError("System Events unavailable")
            with self.assertRaises(ui.WeChatNotFrontmost):
                ui._running_wechat_pid()


class ForegroundOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.handles = {42: f"42:1.5:{ui.WECHAT_BUNDLE_ID}", 99: "99:1.5:com.example.Editor", 77: "77:1.5:com.example.Other"}
        mocks = ExitStack()
        self.addCleanup(mocks.close)
        self.front = mocks.enter_context(patch.object(ui, "_frontmost_pid", return_value=99))
        self.wechat_pid = mocks.enter_context(patch.object(ui, "_running_wechat_pid", return_value=42))
        self.handle = mocks.enter_context(patch.object(ui, "_running_app_handle", side_effect=self.handles.get))
        self.run_script = mocks.enter_context(patch.object(ui, "run"))
        self.run_script.return_value.stdout = "true"

    def test_activate_existing_process_once_and_restore_minimized_roster(self):
        handle = ui.capture_frontmost_app()
        ui.activate_wechat(handle)
        self.wechat_pid.assert_called_once_with()
        self.run_script.assert_called_once()
        script = self.run_script.call_args.args[0][-1]
        self.assertIn('if (unix id of p) is not 42 then return "target_changed"', script)
        self.assertIn('if (count candidates) is not 1 then return "target_changed"', script)
        self.assertIn('if ownerPID is not 99 then return "owner_changed_before_unhide"', script)
        self.assertIn("if not (visible of p) then set visible of p to true", script)
        self.assertEqual(script.count("set frontmost of p to true"), 1)
        self.assertIn('set value of attribute "AXMinimized" of mainWindow to false', script)
        self.assertNotIn('tell application "WeChat"', script)
        self.assertNotIn("keystroke", script)

    def test_activation_refuses_changed_owner_and_does_not_launch_stopped_app(self):
        handle = ui.capture_frontmost_app()
        self.front.return_value = 77
        with self.assertRaises(ui.WeChatNotFrontmost):
            ui.activate_wechat(handle)
        self.front.return_value = 99
        self.wechat_pid.return_value = 0
        with self.assertRaises(ui.WeChatNotFrontmost):
            ui.activate_wechat(handle)
        self.run_script.assert_not_called()

    def test_activation_interrupted_or_rejected_is_not_replayed(self):
        self.run_script.return_value.stdout = "foreground_request_unconfirmed"
        with self.assertRaisesRegex(ui.WeChatNotFrontmost, "foreground_request_unconfirmed"):
            ui.activate_wechat(ui.capture_frontmost_app())
        self.run_script.assert_called_once()

    def test_windowless_reopen_targets_existing_foreground_pid_after_idle_check(self):
        checked = Mock()
        self.run_script.side_effect = [Mock(stdout="no_windows"), Mock(stdout="reopen")]
        with patch.object(ui, "_existing_windows", return_value=[]) as windows, patch.object(ui, "NSAppleEventDescriptor") as desc:
            make_event = desc.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_
            send = make_event.return_value.sendEventWithOptions_timeout_error_
            def submit(options, timeout, error):
                checked.assert_called_once_with()
                self.assertEqual(options, ui.NSAppleEventSendNeverInteract | ui.NSAppleEventSendNoReply)
                return None, None
            send.side_effect = submit
            ui.activate_wechat(ui.capture_frontmost_app(), ensure_idle=checked)
            windows.assert_called_once_with(onscreen_only=True, verified_pid=42)
            desc.descriptorWithProcessIdentifier_.assert_called_once_with(42)
            make_event.assert_called_once_with(0x61657674, 0x72617070, desc.descriptorWithProcessIdentifier_.return_value, -1, 0)
            send.assert_called_once()
            guard = self.run_script.call_args.args[0][-1]
            self.assertIn('first application process whose frontmost is true', guard)
            self.assertIn('(unix id of p) is not 42', guard)
            self.assertIn('if exists window 1 of p then return "window_appeared"', guard)

    def test_no_reopen_over_an_existing_window_new_activity_or_foreground_switch(self):
        with patch.object(ui, "_existing_windows") as windows, patch.object(ui, "NSAppleEventDescriptor") as desc:
            checked = Mock(side_effect=InterruptedError("user active"))
            send = desc.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_.return_value.sendEventWithOptions_timeout_error_
            windows.return_value = [{"window_id": 8}]
            self.run_script.return_value.stdout = "no_windows"
            ui.activate_wechat(ui.capture_frontmost_app(), ensure_idle=checked)
            desc.descriptorWithProcessIdentifier_.assert_not_called()
            windows.return_value = []
            with self.assertRaisesRegex(ui.WeChatNotFrontmost, "without checking user activity"):
                ui.activate_wechat(ui.capture_frontmost_app())
            for verdict in ("owner_changed", "window_appeared", "reopen"):
                self.run_script.side_effect = [Mock(stdout="no_windows"), Mock(stdout=verdict)]
                if verdict == "owner_changed":
                    with self.assertRaisesRegex(ui.WeChatNotFrontmost, "owner_changed"):
                        ui.activate_wechat(ui.capture_frontmost_app(), ensure_idle=checked)
                elif verdict == "reopen":
                    with self.assertRaisesRegex(InterruptedError, "user active"):
                        ui.activate_wechat(ui.capture_frontmost_app(), ensure_idle=checked)
                else:
                    ui.activate_wechat(ui.capture_frontmost_app(), ensure_idle=checked)
            checked.assert_called_once()
            send.assert_not_called()

    def test_hide_targets_only_frontmost_wechat_and_never_hides_other_app(self):
        self.run_script.return_value.stdout = "not_owner"
        self.assertFalse(ui.hide_wechat())
        script = self.run_script.call_args.args[0][-1]
        self.assertLess(script.index("bundle identifier of p"), script.index("set visible of p to false"))
        self.assertNotIn("keystroke", script)
        self.front.return_value = 42
        self.assertEqual(ui.capture_frontmost_app(), "")
        self.run_script.return_value.stdout = "hidden"
        self.assertTrue(ui.hide_wechat())
        self.run_script.return_value.stdout = "hide_unconfirmed"
        with self.assertRaisesRegex(RuntimeError, "confirm"):
            ui.hide_wechat()
        self.run_script.reset_mock()
        ui.restore_frontmost_app("")
        self.run_script.assert_not_called()

    def test_restore_uses_original_live_instance_and_leaves_relaunched_app_alone(self):
        handle = ui.capture_frontmost_app()
        self.front.return_value = 77
        for replacement in ("", "99:2.0:com.example.Editor", "99:1.5:com.example.NewApp"):
            self.handles[99] = replacement
            ui.restore_frontmost_app(handle)
        self.run_script.assert_not_called()
        self.handles[99] = handle
        ui.restore_frontmost_app(handle)
        self.run_script.assert_called_once()
        self.assertIn("first application process whose unix id is 99", self.run_script.call_args.args[0][-1])
        self.assertIn("is not 77 then return false", self.run_script.call_args.args[0][-1])

    def test_capture_defers_if_original_process_identity_unavailable(self):
        self.handles[99] = ""
        with self.assertRaisesRegex(ui.WeChatNotFrontmost, "original foreground process"):
            ui.capture_frontmost_app()
        self.run_script.assert_not_called()


if __name__ == "__main__":
    unittest.main()
