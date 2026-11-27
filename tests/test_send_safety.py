from __future__ import annotations

import copy
import subprocess
import unittest
from unittest.mock import patch

from apps.gateway import cli as gateway
from selftest import FakeIdle, FakeLLM, FakeUI, MemoryStore
from wechat_autoreply.orchestrator import AutoReplyRunner, fingerprint
from wechat_autoreply import peekaboo_utils, wechat_ui


class SendSafetyTests(unittest.TestCase):
    def test_replacement_draft_does_not_inherit_previous_delivery_timeout(self) -> None:
        for attempts in (0, 1):
            with self.subTest(attempts=attempts):
                store = MemoryStore()
                store.config.update(contact_memory_enabled=False, recheck_vote_frames=1)
                old = {
                    "contact": "May", "inbound_text": "old question", "draft_text": "old answer",
                    "inbound_fingerprint": fingerprint("May", "old question"),
                    "send_attempts": attempts, "last_send_attempt_at": 100.0,
                    "send_confirmation_attempts": 1, "selection_retry_count": 5,
                    "selection_snooze_count": 2,
                }
                other = {"contact": "Ted Liu", "inbound_fingerprint": "other", "due_at": 1000.0}
                state = {"pending_queue": [other, old], "recent_auto_outbounds": {"May": ["keep"]}}
                ui = FakeUI([
                    {"status": "ok", "selectionConfirmed": True, "activeChat": "May",
                     "chatPanel": {"latestInbound": "new question"}},
                    {"status": "ok", "selectionConfirmed": True, "activeChat": "May",
                     "chatPanel": {"latestOutbound": "new answer"}},
                ])
                runner = AutoReplyRunner(
                    ui=ui, idle_sensor=FakeIdle(60), llm_client=FakeLLM("new answer"),
                    save_state_fn=store.save_state, append_event_fn=store.append_event,
                    now_fn=lambda: 500.0,
                )
                with patch.object(runner, "_generate_guarded_reply", return_value="new answer"):
                    updated = runner._refresh_pending(
                        store.config, state, state["pending_queue"], 1, "new question", "", {}, 60, 200,
                        reason="latest_message_seen", refresh_delay_seconds=300,
                    )
                self.assertEqual(state["pending_queue"], [other, updated])
                self.assertEqual(old["draft_text"], "old answer")
                for key in ("send_attempts", "last_send_attempt_at", "send_confirmation_attempts",
                            "selection_retry_count", "selection_snooze_count"):
                    self.assertNotIn(key, updated)
                records = state.get("unconfirmed_sends", [])
                self.assertEqual(len(records), attempts)
                if attempts:
                    self.assertEqual(records[0]["draft_text"], "old answer")
                    self.assertEqual(records[0]["inbound_fingerprint"], old["inbound_fingerprint"])
                self.assertEqual(state["recent_auto_outbounds"], {"May": ["keep"]})
                state.pop("recent_auto_outbounds")
                result = runner._handle_pending(store.config, state, updated, 60, 301)
                self.assertEqual(result["status"], "pending_wait_delay")
                self.assertEqual(ui.calls, [])
                result = runner._handle_pending(store.config, state, updated, 60, 500)
                self.assertEqual(result["status"], "sent")
                self.assertEqual(ui.calls.count("send"), 1)
                self.assertEqual(state["pending_queue"], [other])

    def test_changed_input_votes_cannot_fall_through_to_stale_send(self) -> None:
        for readings, expected in (
            (["different one", "different two", "different three"], "pending_retry_selection_not_confirmed"),
            (["different one", "hello", "hello"], "sent"),
            (["different one"] * 3, "pending_refreshed"),
        ):
            with self.subTest(readings=readings):
                store = MemoryStore()
                store.config.update(contact_memory_enabled=False, recheck_vote_frames=1)
                pending = {"contact": "May", "inbound_text": "hello", "draft_text": "hi there",
                           "inbound_fingerprint": fingerprint("May", "hello"), "due_at": 900.0}
                other = {"contact": "Ted Liu", "inbound_fingerprint": "other", "due_at": 2000.0}
                state = {"pending_queue": [other, pending]}
                probes = [{"status": "ok", "selectionConfirmed": True, "activeChat": "May",
                           "chatPanel": {"latestInbound": text}} for text in readings]
                probes.append({"status": "ok", "selectionConfirmed": True, "activeChat": "May",
                               "chatPanel": {"latestOutbound": "hi there"}})
                ui = FakeUI(probes)
                runner = AutoReplyRunner(
                    ui=ui, idle_sensor=FakeIdle(60), llm_client=FakeLLM("new answer"),
                    save_state_fn=store.save_state, append_event_fn=store.append_event,
                    now_fn=lambda: 1000.0,
                )
                result = runner._handle_pending(store.config, state, pending, 60, 1000)
                self.assertEqual(result["status"], expected)
                self.assertEqual(ui.calls.count("send"), int(expected == "sent"))
                self.assertEqual(state["pending_queue"][0], other)
                if expected == "pending_retry_selection_not_confirmed":
                    self.assertEqual(result["reason"], "unstable_changed_inbound")
                    self.assertEqual(state["pending_queue"][1]["draft_text"], "hi there")
                    self.assertGreater(state["pending_queue"][1]["due_at"], 1000)
                elif expected == "pending_refreshed":
                    self.assertEqual(state["pending_queue"][1]["contact"], "May")
                    self.assertEqual(state["pending_queue"][1]["inbound_text"], "different one")

    def test_uncertain_send_is_visible_even_when_queue_is_empty(self) -> None:
        state = {"pending_queue": [], "unconfirmed_sends": [{"contact": "May", "draft_text": "hi"}]}
        self.assertIn("待核实", gateway.queue_output({"enabled": True}, state))
        self.assertIn("May", gateway.queue_output({"enabled": True}, state))
        event = {"type": "send_unconfirmed", "contact": "May", "draft_text": "hi"}
        self.assertIn("待核实", gateway._format_trace(event))
        event.update(type="auto_sent", inferred=True)
        self.assertNotIn("已发送", gateway._format_trace(event))

    def test_sent_count_excludes_legacy_inferences(self) -> None:
        import json
        state = {"last_since_check_at": "2026-11-17T10:00:00+08:00"}
        events = [
            {"type": "auto_sent", "ts": "2026-11-17T10:01:00+08:00", "inferred": True},
            {"type": "auto_sent", "ts": "2026-11-17T10:02:00+08:00", "contact": "May"},
            {"type": "send_unconfirmed", "ts": "2026-11-17T10:03:00+08:00"},
        ]
        with patch.object(gateway, "EVENTS_PATH") as path, patch.object(
            gateway, "read_event_lines", return_value=[json.dumps(e) for e in events]
        ), patch.object(gateway, "save_state"), patch.object(
            gateway, "utc_now_iso", return_value="2026-11-17T11:00:00+08:00"
        ):
            path.exists.return_value = True
            output, _ = gateway.sent_since_output({"enabled": True}, state)
        self.assertIn("自动发送 1 条", output)

    def test_send_intent_is_saved_before_uncertain_keypress(self) -> None:
        for previous_attempts, persist_fails in ((0, False), (1, False), (0, True)):
            with self.subTest(previous_attempts=previous_attempts, persist_fails=persist_fails):
                store = MemoryStore()
                store.config.update(contact_memory_enabled=False, recheck_vote_frames=1)
                pending = {
                    "contact": "May", "inbound_text": "hello", "draft_text": "hi there",
                    "inbound_fingerprint": fingerprint("May", "hello"),
                    "created_at": 800.0, "due_at": 900.0,
                    "send_attempts": previous_attempts,
                }
                store.state["pending_queue"] = [pending]
                ui = FakeUI([{
                    "status": "ok", "selectionConfirmed": True, "activeChat": "May",
                    "chatPanel": {"latestInbound": "hello"},
                }])
                ui.read_input_box_text = lambda *args, **kwargs: "hi there" if previous_attempts else ""

                def save_state(state):
                    if persist_fails:
                        raise RuntimeError("disk write failed")
                    store.save_state(state)

                runner = AutoReplyRunner(
                    idle_sensor=FakeIdle(60), ui=ui,
                    save_state_fn=save_state, append_event_fn=store.append_event,
                    now_fn=lambda: 1050.0,
                )
                observed = []

                def uncertain_send(window_id):
                    self.assertEqual(window_id, 73)
                    observed.append(copy.deepcopy(store.state["pending_queue"][0]))
                    raise RuntimeError("keypress may have landed before transport failed")

                with patch.object(runner, "_send_message_ui", side_effect=uncertain_send):
                    with self.assertRaisesRegex(RuntimeError, "disk write failed|transport failed"):
                        runner._handle_pending(store.config, store.state, pending, 60.0, 1000.0)
                if persist_fails:
                    self.assertEqual(observed, [])
                    continue
                self.assertEqual(observed[0]["send_attempts"], previous_attempts + 1)
                self.assertEqual(observed[0]["last_send_attempt_at"], 1050.0)

    def test_uncertain_send_is_retained_for_review_not_reported_as_sent(self) -> None:
        store = MemoryStore()
        pending = {"contact": "May", "draft_text": "hi", "inbound_fingerprint": "fp",
                   "send_attempts": 1, "last_send_attempt_at": 100.0, "due_at": 150.0}
        state = {"pending_queue": [pending]}
        ui = FakeUI([])
        runner = AutoReplyRunner(ui=ui, idle_sensor=FakeIdle(0), append_event_fn=store.append_event,
                                 save_state_fn=store.save_state, now_fn=lambda: 400.0)
        with patch.object(runner, "_remember_contact_memory") as memory:
            result = runner._handle_pending(store.config, state, pending, 0, 400.0)
        self.assertEqual(result["status"], "send_unconfirmed")
        self.assertEqual(state["pending_queue"], [])
        self.assertEqual(state["unconfirmed_sends"][0]["draft_text"], "hi")
        self.assertFalse(any(e["type"] == "auto_sent" for e in store.events))
        memory.assert_not_called()
        self.assertEqual(ui.calls, [])

    def test_mutating_commands_do_not_retry_an_ambiguous_transport_error(self) -> None:
        for action, args in ((wechat_ui.send_message, ()), (wechat_ui.paste_text, ("hello",))):
            with self.subTest(action=action.__name__), patch.object(
                wechat_ui, "peekaboo_commands", return_value=[["first"], ["fallback"]]
            ), patch.object(peekaboo_utils, "run", side_effect=subprocess.TimeoutExpired("first", 10)) as run:
                with self.assertRaises(RuntimeError):
                    action(*args)
                self.assertEqual(run.call_count, 1)

    def test_read_commands_keep_transport_fallback(self) -> None:
        success = subprocess.CompletedProcess([], 0, "ok", "")
        with patch.object(peekaboo_utils, "run", side_effect=[RuntimeError("unavailable"), success]) as run:
            self.assertIs(peekaboo_utils.run_peekaboo_variants([["first"], ["fallback"]]), success)
        self.assertEqual(run.call_count, 2)
