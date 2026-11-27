from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from wechat_autoreply import wechat_ui as ui


def app(pid, bundle, started=1.5):
    instance = Mock()
    instance.processIdentifier.return_value = pid
    instance.bundleIdentifier.return_value = bundle
    instance.launchDate.return_value.timeIntervalSince1970.return_value = started
    instance.isTerminated.return_value = instance.isHidden.return_value = False
    return instance


class ForegroundOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.wechat = app(42, ui.WECHAT_BUNDLE_ID)
        self.original = app(99, "com.example.Editor")
        mocks = ExitStack()
        self.addCleanup(mocks.close)
        workspace = mocks.enter_context(patch.object(ui, "NSWorkspace"))
        self.front = workspace.sharedWorkspace.return_value.frontmostApplication
        self.front.return_value = self.original
        self.apps = mocks.enter_context(patch.object(ui, "NSRunningApplication"))
        self.apps.runningApplicationsWithBundleIdentifier_.return_value = [self.wechat]
        self.run_script = mocks.enter_context(patch.object(ui, "run"))
        self.run_script.return_value.stdout = "true"

    def test_activate_existing_process_once_and_restore_minimized_roster(self):
        handle = ui.capture_frontmost_app()
        ui.activate_wechat(handle)
        self.apps.runningApplicationsWithBundleIdentifier_.assert_called_once_with(ui.WECHAT_BUNDLE_ID)
        self.run_script.assert_called_once()
        script = self.run_script.call_args.args[0][-1]
        self.assertIn("first application process whose unix id is 42", script)
        self.assertIn("if ownerPID is not 99 then return false", script)
        self.assertIn("if not (visible of p) then set visible of p to true", script)
        self.assertEqual(script.count("set frontmost of p to true"), 1)
        self.assertIn('set value of attribute "AXMinimized" of mainWindow to false', script)
        self.assertNotIn('tell application "WeChat"', script)
        self.assertNotIn("keystroke", script)

    def test_activation_refuses_changed_owner_and_does_not_launch_stopped_app(self):
        handle = ui.capture_frontmost_app()
        self.front.return_value = app(77, "com.example.Other")
        with self.assertRaises(ui.WeChatNotFrontmost):
            ui.activate_wechat(handle)
        self.front.return_value = self.original
        self.apps.runningApplicationsWithBundleIdentifier_.return_value = []
        with self.assertRaises(ui.WeChatNotFrontmost):
            ui.activate_wechat(handle)
        self.run_script.assert_not_called()

    def test_activation_interrupted_or_rejected_is_not_replayed(self):
        self.run_script.return_value.stdout = "false"
        with self.assertRaises(ui.WeChatNotFrontmost):
            ui.activate_wechat(ui.capture_frontmost_app())
        self.run_script.assert_called_once()

    def test_hide_targets_only_frontmost_wechat_and_never_hides_other_app(self):
        self.assertFalse(ui.hide_wechat())
        self.original.hide.assert_not_called()
        self.front.return_value = self.wechat
        self.assertEqual(ui.capture_frontmost_app(), "")
        self.wechat.hide.return_value = True
        self.assertTrue(ui.hide_wechat())
        self.wechat.hide.assert_called_once()
        ui.restore_frontmost_app("")
        self.run_script.assert_not_called()

    def test_restore_uses_original_live_instance_and_leaves_relaunched_app_alone(self):
        handle = ui.capture_frontmost_app()
        self.front.return_value = app(77, "com.example.Other")
        lookup = self.apps.runningApplicationWithProcessIdentifier_
        for replacement in (None, app(99, "com.example.Editor", started=2.0), app(99, "com.example.NewApp")):
            lookup.return_value = replacement
            ui.restore_frontmost_app(handle)
        self.run_script.assert_not_called()
        lookup.return_value = self.original
        ui.restore_frontmost_app(handle)
        self.run_script.assert_called_once()
        self.assertIn("first application process whose unix id is 99", self.run_script.call_args.args[0][-1])
        self.assertIn("is not 77 then return false", self.run_script.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()
