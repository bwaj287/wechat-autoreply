from __future__ import annotations

import unittest
from unittest.mock import patch

from selftest import FakeLLM, FakeUI, FakeVision, MemoryStore
from wechat_autoreply.orchestrator import AutoReplyRunner, _UserActivityPause, fingerprint
from wechat_autoreply import wechat_ui


class Idle:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def get_idle_time_seconds(self) -> float:
        return self.seconds


class UserActivityPauseTests(unittest.TestCase):
    def test_window_capture_waits_for_passive_recovery_with_queue_intact(self):
        store = MemoryStore()
        clock = [1000.]
        store.state["pending_queue"] = [{"contact": "May", "due_at": 900., "draft_text": "hello"}]
        runner = AutoReplyRunner(vision_sensor=FakeVision([]), idle_sensor=Idle(60), ui=FakeUI([]),
                                 load_config_fn=store.load_config, load_state_fn=store.load_state,
                                 save_state_fn=store.save_state, append_event_fn=store.append_event,
                                 now_fn=lambda: clock[0])
        with patch.object(runner, "_handle_pending", side_effect=[
            RuntimeError("could not create image from window"), {"status": "rechecked"}
        ]) as handle, patch.object(runner, "_probe_roster_background_ui",
                                   side_effect=[RuntimeError("still unavailable"), {"status": "ok"}]) as probe:
            for clock[0] in (1000., 1005., 1060., 1065.):
                self.assertEqual(runner.tick()["status"], "waiting_for_window_capture")
                self.assertEqual(store.state["pending_queue"][0]["draft_text"], "hello")
            self.assertEqual((handle.call_count, probe.call_count), (1, 1))
            clock[0] = 1120.
            self.assertEqual(runner.tick()["status"], "rechecked")
            self.assertNotIn("capture_retry_at", store.state)

    def test_interrupted_pending_does_not_schedule_an_unrelated_claim(self) -> None:
        for phase in ("claim", "pending"):
            with self.subTest(phase=phase):
                store = MemoryStore()
                store.state["last_capture_cleanup_at"] = 1000.0
                if phase == "pending":
                    store.state["pending_queue"] = [{"contact": "May", "due_at": 900.0}]
                runner = AutoReplyRunner(
                    vision_sensor=FakeVision([phase == "claim"]),
                    idle_sensor=Idle(60), ui=FakeUI([]),
                    load_config_fn=store.load_config,
                    load_state_fn=store.load_state,
                    save_state_fn=store.save_state,
                    append_event_fn=store.append_event,
                    now_fn=lambda: 1000.0,
                )
                with patch.object(runner, "_handle_" + phase, side_effect=_UserActivityPause):
                    result = runner.tick()
                self.assertIn(result["status"], {"idle_wait", "pending_wait_user_active"})
                self.assertEqual(bool(store.state.get("claim_retry_pending")), phase == "claim")
                self.assertEqual(len(store.state["pending_queue"]), int(phase == "pending"))
                self.assertIn("ui_paused_user_active", [event["type"] for event in store.events])

    def test_pending_pause_propagates_without_cancelling_or_refreshing(self) -> None:
        for phase in ("recheck_vote", "change_vote", "input"):
            with self.subTest(phase=phase):
                store = MemoryStore()
                store.config.update(contact_memory_enabled=False, recheck_vote_frames=1)
                pending = {
                    "contact": "May", "inbound_text": "hello", "message_time": "",
                    "inbound_fingerprint": fingerprint("May", "hello", ""),
                    "draft_text": "hi", "created_at": 800.0, "due_at": 900.0,
                }
                state = {"pending_queue": [pending]}
                snapshot = {
                    "status": "ok", "selectionConfirmed": True, "activeChat": "May",
                    "chatPanel": {"latestInbound": "new question" if phase == "change_vote" else "hello"},
                }
                if phase == "recheck_vote":
                    store.config["recheck_vote_frames"] = 2
                ui = FakeUI([])
                ui.read_input_box_text = lambda *args: ""
                runner = AutoReplyRunner(
                    ui=ui,
                    idle_sensor=Idle(60), llm_client=FakeLLM("replacement"),
                    save_state_fn=store.save_state, append_event_fn=store.append_event,
                    now_fn=lambda: 1000.0,
                )
                with patch.object(runner, "_probe_ui", side_effect=[snapshot, _UserActivityPause()]), patch.object(
                    runner, "_read_input_box_text_ui", side_effect=_UserActivityPause
                ):
                    with self.assertRaises(_UserActivityPause):
                        runner._handle_pending(store.config, state, pending, 60.0, 1000.0)
                self.assertEqual(state["pending_queue"], [pending])
                self.assertEqual(pending["draft_text"], "hi")
                self.assertEqual(pending["due_at"], 900.0)
                self.assertFalse(any("failed" in e["type"] or e["type"] == "pending_cancelled" for e in store.events))

    def test_pause_during_non_whitelist_clear_propagates(self) -> None:
        def row(name):
            return {"name": name, "preview": "hello", "unread": True,
                    "numericBadge": True, "redPixelCount": 120, "digitPixelCount": 10}

        store = MemoryStore()
        store.config.update(contact_memory_enabled=False, badge_stability_frames=1)
        runner = AutoReplyRunner(
            idle_sensor=Idle(60), ui=FakeUI([]), llm_client=FakeLLM("hi"),
            save_state_fn=store.save_state, append_event_fn=store.append_event,
            now_fn=lambda: 1000.0,
        )
        probes = [
            {"status": "ok", "visibleChats": [row("May")]},
            {"status": "ok", "selectionConfirmed": True, "activeChat": "May",
             "chatPanel": {"latestInbound": "hello"}},
            {"status": "ok", "visibleChats": [row("Other")]},
            _UserActivityPause(),
        ]
        with patch.object(runner, "_probe_ui", side_effect=probes):
            with self.assertRaises(_UserActivityPause):
                runner._handle_claim(store.config, store.state, 60.0, 1000.0)
        self.assertNotIn("non_whitelist_clear_failed", [e["type"] for e in store.events])

    def test_own_input_does_not_pause_but_new_user_input_does(self) -> None:
        clock = {"now": 100.0}
        idle = Idle(45.0)
        runner = AutoReplyRunner(idle_sensor=idle, ui=FakeUI([]), now_fn=lambda: clock["now"])
        calls: list[str] = []

        def own_click() -> None:
            calls.append("click")
            idle.seconds = 0.0

        runner._run_internal_ui_action(own_click)
        clock["now"] = 105.0
        idle.seconds = 5.0
        runner._run_internal_ui_action(lambda: calls.append("next"))
        self.assertEqual(calls, ["click", "next"])

        clock["now"] = 106.0
        idle.seconds = 0.1
        with self.assertRaises(_UserActivityPause):
            runner._run_internal_ui_action(lambda: calls.append("blocked"))
        self.assertEqual(calls, ["click", "next"])
        self.assertTrue(runner._user_activity_pause)

        clock["now"] = 126.0
        idle.seconds = 20.1
        runner._run_internal_ui_action(lambda: calls.append("resumed"))
        self.assertEqual(calls[-1], "resumed")

    def test_probe_checks_activity_between_foreground_actions(self) -> None:
        calls: list[str] = []

        def guard(action, *args, **kwargs):
            if calls:
                raise _UserActivityPause()
            calls.append("activate")
            return action(*args, **kwargs)

        with patch.object(wechat_ui, "activate_wechat") as activate, patch.object(
            wechat_ui, "ensure_main_roster_window"
        ) as focus:
            with self.assertRaises(_UserActivityPause):
                wechat_ui.probe(ui_action=guard)
        activate.assert_called_once_with()
        focus.assert_not_called()


if __name__ == "__main__":
    unittest.main()
