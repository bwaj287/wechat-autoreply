from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wechat_autoreply import wechat_ui as ui


class BackgroundUITests(unittest.TestCase):
    window = {"window_id": 42, "pid": 123, "title": "Weixin", "x": 0, "y": 0, "width": 900, "height": 700}

    def roster(self, *, title="Kai", preview="See you"):
        return {
            "status": "ok", "window": self.window, "screenshot": "/tmp/synthetic.png",
            "activeChat": title, "visibleChats": [{"name": "Kai", "numericBadge": True, "preview": preview}],
            "chatPanel": {"latestInbound": "See you", "inbound": [{"text": "See you"}]},
        }

    def test_mismatched_or_unreadable_foreground_fails_closed(self):
        with patch.object(ui, "NSWorkspace") as workspace, patch.object(
            ui, "_existing_windows", return_value=[self.window]
        ), patch.object(ui, "run") as run:
            app = workspace.sharedWorkspace.return_value.frontmostApplication.return_value
            app.bundleIdentifier.return_value = ui.WECHAT_BUNDLE_ID
            app.processIdentifier.return_value = 123
            app.isHidden.return_value = False
            for answer in ("", "0\nWeixin", "123\nOther Window"):
                with self.subTest(answer=answer):
                    run.return_value.stdout = answer
                    self.assertFalse(ui.is_wechat_frontmost())
            run.side_effect = TimeoutError()
            self.assertFalse(ui.is_wechat_frontmost())

    def test_window_guard_requires_matching_live_pid_title_and_window_id(self):
        app = Mock()
        app.bundleIdentifier.return_value = ui.WECHAT_BUNDLE_ID
        app.processIdentifier.return_value = 123
        app.isHidden.return_value = False
        with patch.object(ui, "NSWorkspace") as workspace, patch.object(
            ui, "_existing_windows", return_value=[self.window]
        ), patch.object(ui, "run", return_value=SimpleNamespace(stdout="123\nWeixin")):
            workspace.sharedWorkspace.return_value.frontmostApplication.return_value = app
            self.assertEqual(ui._require_front_window(42), self.window)
            with self.assertRaises(ui.WeChatNotFrontmost):
                ui._require_front_window(99)
            app.isHidden.return_value = True
            self.assertFalse(ui.is_wechat_frontmost())

    def test_background_capture_only_uses_existing_window_id(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ui, "run") as run, patch.object(
            ui, "run_peekaboo_variants", side_effect=AssertionError("passive capture must not use app targeting")
        ):
            image = Path(tmp) / "panel.png"
            ui.capture_window(image, self.window, passive=True)
            run.assert_called_once_with(["/usr/sbin/screencapture", "-x", "-o", "-l42", str(image)], timeout=10)
            with self.assertRaises(RuntimeError):
                ui.capture_window(image, {"title": "Weixin"}, passive=True)

    def test_background_requires_matching_header_stable_bubbles_and_unread_preview(self):
        for preview, header, second, ok in (
            ("See you", "Kai", "See you", True),
            ("See yo…", "Kai", "See you", True),
            ("Earlier", "Kai", "See you", False),
            ("See you", "Other", "See you", False),
            ("See you", "Kai", "New message", False),
        ):
            roster = self.roster(preview=preview)
            with self.subTest(preview=preview, header=header, second=second), patch.object(
                ui, "probe_roster_background", return_value=roster
            ), patch.object(ui, "capture_window") as capture, patch.object(
                ui, "_prepare_ocr_items", return_value=[]
            ), patch.object(ui, "_pick_selected_title", return_value=header), patch.object(
                ui, "_extract_chat_panel", return_value={"inbound": [{"text": second}], "latestInbound": second}
            ), patch.object(ui, "_strict_interaction", side_effect=AssertionError("background input")):
                result = ui.probe_chat_background("Kai")
                self.assertIs(result["selectionConfirmed"], ok)
                self.assertTrue(capture.call_args.kwargs["passive"])

    def test_wrong_detached_window_and_ambiguous_row_do_not_trigger_selection(self):
        roster = self.roster(title="Other")
        with patch.object(ui, "probe_roster_background", return_value=roster), patch.object(
            ui, "list_wechat_windows", return_value=[dict(self.window, title="Kailee")]
        ), patch.object(ui, "capture_window") as capture, patch.object(ui, "_strict_interaction") as interact:
            self.assertFalse(ui.probe_chat_background("Kai")["selectionConfirmed"])
            roster["visibleChats"] *= 2
            self.assertFalse(ui.probe_chat_background("Kai")["selectionConfirmed"])
            capture.assert_not_called()
            interact.assert_not_called()

    def test_foreground_recovers_service_page_and_searches_only_verified_field_and_row(self):
        roster = self.roster(title="Other")
        roster["visibleChats"] = []
        resolved = self.roster()
        result_row = {"name": "Kai", "click": {"x": 140, "y": 175}}
        captures = [
            {"status": "service_accounts", "serviceBack": (40, 60), "window": self.window},
            roster, {**roster, "visibleChats": [result_row]}, resolved,
        ]
        with patch.object(ui, "_require_front_window", return_value=self.window), patch.object(
            ui, "probe_roster_background", side_effect=captures
        ), patch.object(ui, "_search_field_focused", return_value=True), patch.object(
            ui, "_strict_interaction"
        ) as interact, patch.object(ui, "_verified_panel", return_value={"selectionConfirmed": True}), patch.object(ui.time, "sleep"):
            self.assertTrue(ui.probe("Kai", strict_foreground=True)["selectionConfirmed"])
            self.assertEqual([call.args[0][0] for call in interact.call_args_list], ["click", "click", "hotkey", "paste", "click", "press"])
            self.assertTrue(all(call.args[1] == 42 for call in interact.call_args_list))

    def test_search_does_not_type_into_an_unverified_field_or_another_window(self):
        roster = self.roster(title="Other")
        roster["visibleChats"] = []
        with patch.object(ui, "probe_roster_background", return_value=roster), patch.object(
            ui, "_search_field_focused", return_value=False
        ), patch.object(ui, "_strict_interaction") as interact, patch.object(ui.time, "sleep"):
            with patch.object(ui, "_require_front_window", return_value=self.window):
                self.assertFalse(ui.probe("Kai", strict_foreground=True)["selectionConfirmed"])
            self.assertEqual([c.args[0][0] for c in interact.call_args_list], ["click"])
            interact.reset_mock()
            with patch.object(ui, "_require_front_window", return_value=dict(self.window, title="Unrelated", window_id=101)):
                self.assertFalse(ui.probe("Kai", strict_foreground=True)["selectionConfirmed"])
            interact.assert_not_called()

    def test_input_verification_rereads_current_title_in_the_pinned_window(self):
        for detached in (True, False):
            window = dict(self.window, title="Kai" if detached else "Weixin")
            selected = {**self.roster(), "selectionConfirmed": True, "chatWindow": window if detached else None}
            with self.subTest(detached=detached), patch.object(
                ui, "_require_front_window", return_value=window
            ) as guard, patch.object(ui, "capture_window") as capture, patch.object(
                ui, "_prepare_ocr_items", return_value=[]
            ), patch.object(ui, "_pick_chat_window_title" if detached else "_pick_selected_title") as title, patch.object(
                ui, "_strict_interaction"
            ) as interact:
                title.return_value = "Kai"
                ui.verify_chat_for_input(selected, "Kai")
                self.assertEqual([call.args for call in guard.call_args_list], [(42,), (42,)])
                title.return_value = "Elsewhere"
                with self.assertRaises(ui.WeChatNotFrontmost):
                    ui.verify_chat_for_input(selected, "Kai")
                self.assertTrue(capture.call_args.kwargs["passive"])
                interact.assert_not_called()

    def test_strict_send_requires_same_window_and_disables_implicit_focus(self):
        with patch.object(ui, "_require_front_window", return_value=self.window) as guard, patch.object(
            ui, "peekaboo_commands", side_effect=lambda args: [["peekaboo", *args], ["unused"]]
        ), patch.object(ui, "run") as run, patch.object(ui.time, "sleep"):
            for fn, args, command in ((ui.paste_text, ("hi",), "paste"), (ui.send_message, (), "press")):
                with self.assertRaises(ui.WeChatNotFrontmost):
                    fn(*args, strict_foreground=True)
                fn(*args, strict_foreground=True, window_id=42)
                guard.assert_called_with(42)
                argv = run.call_args.args[0]
                self.assertEqual(argv[1], command)
                self.assertEqual(argv[-6:], ["--app", "PID:123", "--window-id", "42", "--no-auto-focus", "--json"])
            guard.side_effect = ui.WeChatNotFrontmost()
            with self.assertRaises(ui.WeChatNotFrontmost):
                ui.probe(select_chat="Kai", strict_foreground=True)
            self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
