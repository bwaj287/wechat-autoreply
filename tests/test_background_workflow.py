from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from selftest import FakeBackgroundUI, FakeIdle, FakeLLM, FakeVision, MemoryStore
from wechat_autoreply.orchestrator import AutoReplyRunner, fingerprint
from wechat_autoreply.wechat_ui import WeChatNotFrontmost


def panel(**messages):
    return {"status": "ok", "activeChat": "May", "selectionConfirmed": True, "chatPanel": messages}


BADGE = {
    "name": "May", "preview": "hello", "unread": True,
    "numericBadge": True, "redPixelCount": 120, "digitPixelCount": 10,
}


class BackgroundWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.config.update(badge_stability_frames=1, pending_stale_ttl_seconds=30)
        self.now = 1000.0
        self.store.state["last_capture_cleanup_at"] = self.now
        self.ui = FakeBackgroundUI([], [])
        self.ui.frontmost = False
        self.vision = FakeVision([])
        self.idle = FakeIdle(60)
        self.llm = FakeLLM("hi there")

    def runner(self):
        return AutoReplyRunner(
            ui=self.ui, vision_sensor=self.vision, idle_sensor=self.idle, llm_client=self.llm,
            load_config_fn=self.store.load_config, load_state_fn=self.store.load_state,
            save_state_fn=self.store.save_state, append_event_fn=self.store.append_event,
            now_fn=lambda: self.now,
        )

    def add_due_draft(self):
        self.store.state["pending_queue"] = [{
            "contact": "May", "inbound_text": "hello", "draft_text": "hi there",
            "inbound_fingerprint": fingerprint("May", "hello"),
            "created_at": self.now - 20, "due_at": self.now - 1,
        }]

    def tearDown(self):
        for call in self.ui.calls:
            self.assertNotIn(call if isinstance(call, str) else call[0], {"activate", "hide", "restore"})
        self.assertFalse(any(e["type"] == "wechat_window_action" for e in self.store.events))

    def test_read_only_badge_survives_restart_and_disappearing_digit(self):
        self.store.config["passive_roster_sweep_enabled"] = True
        self.idle.value = 1
        self.ui.background_probes = [{"status": "ok", "activeChat": "Darren", "visibleChats": [BADGE]}]
        self.assertEqual(self.runner().tick()["status"], "waiting_for_wechat")
        self.assertIn("May", self.store.state["background_claim_opportunities"])
        self.assertEqual(self.ui.calls, [("probe_background", None)])
        self.assertEqual(self.llm.calls, [])

        self.now += 5
        self.ui.frontmost = True
        self.runner().tick()
        self.assertIn("May", self.store.state["background_claim_opportunities"])
        self.assertEqual(self.ui.calls, [("probe_background", None)])

        self.now += 20
        self.idle.value = 21
        self.ui.probes = [
            {"status": "ok", "visibleChats": [{"name": "May", "preview": "hello", "unread": False}]},
            panel(latestInbound="hello", inbound=[{"text": "hello", "top": .65, "left": .32,
                                                   "width": .13, "grayPixels": 80}]),
            {"status": "ok", "visibleChats": []},
        ]
        result = self.runner().tick()
        self.assertEqual(result["status"], "draft_saved", result)
        self.assertEqual(self.store.state["pending"]["draft_text"], "hi there")
        self.assertEqual(self.llm.calls, [("May", "hello")])
        self.assertNotIn("May", self.store.state["background_claim_opportunities"])

    def test_exact_saved_contact_now_offscreen_is_selected_and_verified(self):
        self.store.config["passive_roster_sweep_enabled"] = True
        self.ui.background_probes = [{"status": "ok", "visibleChats": [BADGE]}]
        self.assertEqual(self.runner().tick()["status"], "waiting_for_wechat")
        self.ui.frontmost = True
        self.now += 2
        self.ui.probes = [
            {"status": "ok", "visibleChats": [{"name": "Darren", "preview": "other chat"}]},
            panel(title="May", latestInbound="hello", inbound=[
                {"text": "hello", "top": .65, "left": .32, "width": .13, "grayPixels": 80}
            ]), {"status": "ok", "visibleChats": []},
        ]
        result = self.runner().tick()
        self.assertEqual(result["status"], "draft_saved", result)
        self.assertIn(("probe", "May"), self.ui.calls)
        self.assertEqual(self.llm.calls, [("May", "hello")])
        self.assertNotIn("May", self.store.state["background_claim_opportunities"])

    def test_menu_only_trigger_waits_across_restart_until_foreground_and_idle(self):
        self.vision.values = ["1", "", ""]
        self.ui.background_probes = [{"status": "ok", "visibleChats": []}]
        self.assertEqual(self.runner().tick()["status"], "waiting_for_wechat")
        self.assertTrue(self.store.state["foreground_scan_pending"])
        self.assertEqual(self.ui.calls, [("probe_background", None)])
        self.now += 20
        self.ui.frontmost = True
        self.idle.value = 1
        self.runner().tick()
        self.assertTrue(self.store.state["foreground_scan_pending"])
        self.assertEqual(self.ui.calls, [("probe_background", None)])
        self.now += 30
        self.idle.value = 30
        self.ui.probes = [{"status": "ok", "visibleChats": []}]
        result = self.runner().tick()
        self.assertEqual(result["status"], "no_candidate", result)
        self.assertEqual(self.ui.calls, [("probe_background", None), ("probe", None)])
        self.assertFalse(self.store.state["foreground_scan_pending"])

    def test_due_draft_waits_past_ttl_and_sends_once_after_restart(self):
        self.add_due_draft()
        self.assertEqual(self.runner().tick()["status"], "waiting_for_wechat")
        self.now += 120
        self.assertEqual(self.runner().tick()["status"], "waiting_for_wechat")
        self.assertEqual(self.store.state["pending"]["draft_text"], "hi there")
        self.ui.frontmost = True
        self.idle.value = 0
        self.runner().tick()
        self.assertEqual(self.store.state["pending"]["draft_text"], "hi there")
        self.assertEqual(self.ui.calls, [])

        self.now += 30
        self.idle.value = 30
        self.ui.probes = [panel(latestInbound="hello"), panel(latestOutbound="hi there")]
        result = self.runner().tick()
        self.assertEqual(result["status"], "sent", result)
        self.assertEqual(self.store.state["pending_queue"], [])
        self.assertEqual(self.ui.calls.count("send"), 1)
        self.assertIn(("probe", "May"), self.ui.calls)

    def interrupt_at(self, step):
        self.add_due_draft()
        self.ui.frontmost = True
        self.ui.probes = [panel(latestInbound="hello")]
        original = getattr(self.ui, step)

        def switch(*args, **kwargs):
            original(*args, **kwargs)
            self.ui.frontmost = False

        with patch.object(self.ui, step, side_effect=switch):
            result = self.runner().tick()
        self.assertEqual(result["status"], "waiting_for_wechat", result)
        self.assertNotIn("send", self.ui.calls)
        self.assertEqual(self.store.state["pending"]["draft_text"], "hi there")
        self.assertFalse(any(e["type"] == "auto_sent" for e in self.store.events))

    def test_switch_away_after_focus_stops_before_paste(self):
        self.interrupt_at("focus_input_box")
        self.assertNotIn(("paste", "hi there"), self.ui.calls)

    def test_switch_after_paste_resumes_without_duplicate_paste_or_return(self):
        self.interrupt_at("paste_text")
        self.assertEqual(self.store.state["pending"].get("send_attempts", 0), 0)
        self.ui.frontmost = True
        self.now += 5
        self.ui.read_input_box_text = Mock(return_value="hi there")
        self.ui.probes = [panel(latestInbound="hello"), panel(latestOutbound="hi there")]
        resumed = self.runner().tick()
        self.assertEqual(resumed["status"], "sent", resumed)
        self.ui.read_input_box_text.assert_called_once()
        self.assertEqual(self.ui.calls.count(("paste", "hi there")), 1)
        self.assertEqual(self.ui.calls.count("send"), 1)

    def test_modified_prepared_draft_is_left_for_the_user(self):
        self.interrupt_at("paste_text")
        self.ui.frontmost = True
        self.now += 5
        self.ui.read_input_box_text = Mock(return_value="my own reply")
        self.ui.probes = [panel(latestInbound="hello")]
        result = self.runner().tick()
        self.assertEqual(result.get("reason"), "input_box_modified", result)
        self.ui.read_input_box_text.assert_called_once()
        self.assertEqual(self.ui.calls.count(("paste", "hi there")), 1)
        self.assertNotIn("send", self.ui.calls)

    def test_switch_after_return_rechecks_the_send_without_repeating_it(self):
        self.add_due_draft()
        self.ui.frontmost = True
        self.ui.probes = [panel(latestInbound="hello")]
        send = self.ui.send_message

        def switch_after_send(**kwargs):
            send(**kwargs)
            self.ui.frontmost = False

        with patch.object(self.ui, "send_message", side_effect=switch_after_send):
            result = self.runner().tick()
        self.assertEqual(result["status"], "waiting_for_wechat", result)
        self.assertEqual(self.store.state["pending"]["send_attempts"], 1)
        self.assertEqual(self.ui.calls.count("send"), 1)

        self.now += self.store.config["send_confirmation_timeout_seconds"] + 1
        self.ui.frontmost = True
        self.ui.probes = [panel(latestInbound="hello", latestOutbound="hi there")]
        result = self.runner().tick()
        self.assertEqual(result["status"], "sent", result)
        self.assertEqual(self.store.state["pending_queue"], [])
        self.assertEqual(self.ui.calls.count("send"), 1)

    def test_user_switch_in_same_window_during_ocr_stops_before_input(self):
        self.add_due_draft()
        self.ui.frontmost = True
        self.ui.probes = [panel(latestInbound="hello")]
        probe = self.ui.probe

        def switched_during_read(*args, **kwargs):
            previous_chat = probe(*args, **kwargs)
            self.now += 4
            self.idle.value = .1
            return previous_chat

        with patch.object(self.ui, "probe", side_effect=switched_during_read):
            result = self.runner().tick()
        self.assertEqual(result["status"], "pending_wait_user_active", result)
        self.assertEqual(self.ui.calls, [("probe", "May")])
        self.assertEqual(self.store.state["pending"]["draft_text"], "hi there")

    def test_same_window_must_still_show_recipient_before_paste_and_return(self):
        for switch_at in ("focus_input_box", "paste_text"):
            with self.subTest(switch_at=switch_at):
                self.add_due_draft()
                self.ui.frontmost = True
                self.ui.calls.clear()
                self.ui.probes = [panel(latestInbound="hello")]
                active_chat = "May"
                action = getattr(self.ui, switch_at)

                def switch_chat(*args, **kwargs):
                    nonlocal active_chat
                    action(*args, **kwargs)
                    active_chat = "Darren"

                def verify_chat(selected, contact):
                    self.assertEqual(selected["window"]["window_id"], 73)
                    self.assertEqual(contact, "May")
                    if active_chat != contact:
                        raise WeChatNotFrontmost()

                with patch.object(self.ui, switch_at, side_effect=switch_chat), patch.object(
                    self.ui, "verify_chat_for_input", side_effect=verify_chat
                ) as verify:
                    result = self.runner().tick()
                self.assertEqual(result["status"], "waiting_for_wechat", result)
                self.assertTrue(verify.called)
                self.assertEqual(self.ui.calls.count(("paste", "hi there")), int(switch_at == "paste_text"))
                self.assertNotIn("send", self.ui.calls)
                self.assertEqual(self.store.state["pending"]["draft_text"], "hi there")

    def test_failed_frontmost_lookup_cannot_authorize_input(self):
        self.add_due_draft()
        self.ui.frontmost = True
        with patch.object(self.ui, "is_wechat_frontmost", side_effect=RuntimeError("unavailable")):
            result = self.runner().tick()
        self.assertEqual(result["status"], "waiting_for_wechat", result)
        self.assertEqual(self.ui.calls, [])
        self.assertEqual(self.store.state["pending"]["draft_text"], "hi there")

    def test_pending_contact_removed_while_hidden_cannot_receive_a_send(self):
        self.add_due_draft()
        self.assertEqual(self.runner().tick()["status"], "waiting_for_wechat")
        self.store.config["allowed_contacts"].remove("May")
        self.ui.frontmost = True
        result = self.runner().tick()
        self.assertEqual(result.get("reason"), "contact_removed_from_allowlist", result)
        self.assertEqual(self.store.state["pending_queue"], [])
        self.assertEqual(self.ui.calls, [])

    def test_disabled_runner_leaves_existing_work_and_sensors_alone(self):
        self.add_due_draft()
        self.store.state["foreground_scan_pending"] = True
        self.store.config.update(enabled=False, passive_roster_sweep_enabled=True)
        self.ui.is_wechat_frontmost = Mock()
        self.vision.unread_signal = Mock()
        self.idle.get_idle_time_seconds = Mock()
        self.assertEqual(self.runner().tick()["status"], "disabled")
        self.ui.is_wechat_frontmost.assert_not_called()
        self.vision.unread_signal.assert_not_called()
        self.idle.get_idle_time_seconds.assert_not_called()
        self.assertEqual(self.ui.calls, [])
        self.assertEqual(self.store.state["pending_queue"][0]["draft_text"], "hi there")
        self.assertTrue(self.store.state["foreground_scan_pending"])


if __name__ == "__main__":
    unittest.main()
