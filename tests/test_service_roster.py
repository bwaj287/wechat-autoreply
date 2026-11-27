from __future__ import annotations

import unittest
from unittest.mock import patch

from selftest import FakeUI
from wechat_autoreply import wechat_ui
from wechat_autoreply.orchestrator import AutoReplyRunner
from wechat_autoreply.wechat_ui import _is_service_chat_name, _service_accounts_heading, extract_visible_chats


class ServiceRosterTests(unittest.TestCase):
    def test_service_name_matches_full_and_truncated_labels_only(self) -> None:
        for name in (
            "Service",
            "Service Notifications",
            "Servic...",
            "Servic…",
            "Serv...",
            "ServiC... Yesterday 20:00",
        ):
            with self.subTest(name=name):
                self.assertTrue(_is_service_chat_name(name))
        self.assertFalse(_is_service_chat_name("Serviceable"))
        self.assertFalse(_is_service_chat_name("Servant..."))

    def test_service_row_is_not_available_to_click_paths(self) -> None:
        def item(name: str, top: float) -> dict:
            return {
                "text": name,
                "bbox": {"left": 0.15, "top": top, "x": 0.15, "y": 1 - top, "w": 0.10, "h": 0.03},
                "center": {"y": top + 0.015},
            }

        chats = extract_visible_chats(
            [item("Servic...", 0.2), item("May", 0.4)],
            {"x": 0, "y": 0, "width": 1000, "height": 800},
        )
        self.assertEqual([chat["name"] for chat in chats], ["May"])

        service = item("ServiC... Yesterday 20:00", 0.2)
        service["contactText"] = "May"
        self.assertEqual(extract_visible_chats([service], {"x": 0, "y": 0, "width": 1000, "height": 800}), [])

        chats = extract_visible_chats(
            [item("ServiC... Yesterday 20:00", 0.2), item("May", 0.4)],
            {"x": 0, "y": 0, "width": 1000, "height": 800},
        )
        self.assertEqual([chat["name"] for chat in chats], ["May"])

    def test_service_accounts_page_stops_roster_probe(self) -> None:
        heading = {
            "text": "〈 Service Accounts",
            "bbox": {"left": 0.064, "top": 0.074, "h": 0.02},
        }
        self.assertIs(_service_accounts_heading([heading]), heading)
        window = {"title": "Weixin", "x": 0, "y": 0, "width": 1182, "height": 945, "window_id": 73}
        with patch.object(wechat_ui, "list_wechat_windows", return_value=[window]), patch.object(
            wechat_ui, "capture_window"
        ), patch.object(wechat_ui, "_prepare_ocr_items", return_value=[heading]):
            result = wechat_ui.probe_roster_background()
        self.assertEqual(result["status"], "service_accounts")
        self.assertEqual(result["visibleChats"], [])

    def test_service_accounts_recovery_only_clicks_back_once(self) -> None:
        heading = {
            "text": "〈 Service Accounts",
            "bbox": {"left": 0.064, "top": 0.074, "h": 0.02},
        }
        window = {"title": "Weixin", "x": 0, "y": 0, "width": 1182, "height": 945, "window_id": 73}
        with patch.object(wechat_ui, "activate_wechat"), patch.object(
            wechat_ui, "ensure_main_roster_window", return_value=window
        ), patch.object(wechat_ui, "capture_window"), patch.object(
            wechat_ui, "_prepare_ocr_items", return_value=[heading]
        ), patch.object(wechat_ui, "click_coords") as click:
            result = wechat_ui.probe()
        self.assertEqual(result["status"], "service_accounts")
        click.assert_called_once()
        self.assertEqual(click.call_args.args[:2], (81, 79))

    def test_service_accounts_preflight_schedules_recovery_without_unread_signal(self) -> None:
        class Idle:
            def get_idle_time_seconds(self) -> float:
                return 60.0

        class Vision:
            def unread_signal(self) -> str:
                return ""

        class UI(FakeUI):
            def probe_roster_background(self) -> dict:
                return {"status": "service_accounts", "visibleChats": []}

        events: list[tuple[str, dict]] = []
        runner = AutoReplyRunner(
            idle_sensor=Idle(),
            vision_sensor=Vision(),
            ui=UI([]),
            load_config_fn=lambda: {
                "enabled": True,
                "idle_threshold_seconds": 20,
                "passive_roster_sweep_enabled": True,
                "roster_sweep_interval_seconds": 60,
                "capture_cleanup_interval_seconds": 1000,
            },
            load_state_fn=lambda: {"last_capture_cleanup_at": 1000.0},
            save_state_fn=lambda state: None,
            append_event_fn=lambda event, **payload: events.append((event, payload)),
            now_fn=lambda: 1000.0,
        )
        with patch.object(runner, "_handle_claim", return_value={"status": "no_candidate"}) as claim:
            result = runner.tick()
        self.assertEqual(result["status"], "no_candidate")
        self.assertEqual(claim.call_args.kwargs["claim_trigger"], "passive_roster_sweep")
        self.assertTrue(any(event == "passive_roster_preflight" and data["page_status"] == "service_accounts" for event, data in events))

    def test_failed_recovery_stops_claim_without_second_probe(self) -> None:
        events: list[str] = []
        runner = AutoReplyRunner(
            ui=FakeUI([]),
            idle_sensor=type("Idle", (), {"get_idle_time_seconds": lambda self: 60.0})(),
            append_event_fn=lambda event, **payload: events.append(event),
        )
        state: dict = {}
        with patch.object(runner, "_probe_ui", return_value={"status": "service_accounts"}) as probe:
            result = runner._handle_claim({"idle_threshold_seconds": 20}, state, 60.0, 1000.0)
        self.assertEqual(result["status"], "service_accounts")
        probe.assert_called_once_with()
        self.assertIn("service_accounts_recovery_deferred", events)


if __name__ == "__main__":
    unittest.main()
