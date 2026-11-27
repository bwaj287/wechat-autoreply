from __future__ import annotations

import unittest
from unittest.mock import patch

from selftest import FakeUI
from wechat_autoreply import wechat_ui
from wechat_autoreply.orchestrator import AutoReplyRunner


class UnreadableRosterTests(unittest.TestCase):
    def test_empty_roster_ocr_never_searches_contact(self) -> None:
        window = {"title": "Weixin", "x": 0, "y": 0, "width": 1182, "height": 945, "window_id": 3998}
        with patch.object(wechat_ui, "activate_wechat"), patch.object(
            wechat_ui, "ensure_main_roster_window", return_value=window
        ), patch.object(wechat_ui, "capture_window"), patch.object(
            wechat_ui, "_prepare_ocr_items", return_value=[]
        ), patch.object(wechat_ui, "_select_chat_via_search") as search:
            result = wechat_ui.probe(select_chat="May")
        self.assertEqual(result["status"], "window_unreadable")
        search.assert_not_called()

    def test_empty_background_roster_is_not_ok(self) -> None:
        window = {"title": "Weixin", "x": 0, "y": 0, "width": 1182, "height": 945, "window_id": 3998}
        with patch.object(wechat_ui, "list_wechat_windows", return_value=[window]), patch.object(
            wechat_ui, "capture_window"
        ), patch.object(wechat_ui, "_prepare_ocr_items", return_value=[]):
            result = wechat_ui.probe_roster_background()
        self.assertEqual(result["status"], "window_unreadable")
        self.assertEqual(result["visibleChats"], [])

    def test_pending_waits_without_researching_when_window_unreadable(self) -> None:
        pending = {"contact": "May", "draft_text": "你好", "due_at": 900.0, "created_at": 800.0}
        state = {"pending_queue": [pending], "pending": pending}
        saved: list[dict] = []
        runner = AutoReplyRunner(
            ui=FakeUI([]),
            idle_sensor=type("Idle", (), {"get_idle_time_seconds": lambda self: 60.0})(),
            save_state_fn=lambda value: saved.append(value.copy()),
            append_event_fn=lambda *args, **kwargs: None,
            now_fn=lambda: 1000.0,
        )
        with patch.object(runner, "_probe_ui", return_value={"status": "window_unreadable"}) as probe:
            result = runner._handle_pending({"allowed_contacts": ["May"], "idle_threshold_seconds": 20}, state, pending, 60.0, 1000.0)
        self.assertEqual(result["status"], "pending_window_unreadable")
        self.assertEqual(pending["due_at"], 1300.0)
        self.assertTrue(state["wechat_window_unavailable"])
        self.assertEqual(len(saved), 1)
        probe.assert_called_once_with(select_chat="May")


if __name__ == "__main__":
    unittest.main()
