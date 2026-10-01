from __future__ import annotations

import unittest

from wechat_autoreply.orchestrator import AutoReplyRunner
from wechat_autoreply.outbound_history import remember_recent_auto_outbound


class SequenceLlm:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.avoid_calls: list[list[str]] = []
        self.context_calls: list[object] = []
        self.memory_calls: list[object] = []

    def generate_reply(self, contact: str, inbound_text: str, *, avoid_replies=None, **kwargs) -> str:
        self.avoid_calls.append(list(avoid_replies or []))
        self.context_calls.append(kwargs.get("conversation_context"))
        self.memory_calls.append(kwargs.get("contact_memory"))
        return self.replies.pop(0)


class DuplicateDraftGuardTests(unittest.TestCase):
    def _state_with_sent_reply(self) -> dict:
        state: dict = {}
        remember_recent_auto_outbound(
            state,
            "May",
            "这句已经发过了",
            now=100,
            source="auto_sent_immediate",
            ttl_seconds=3600,
        )
        return state

    def test_duplicate_draft_is_regenerated_once(self) -> None:
        events: list[tuple[str, dict]] = []
        runner = AutoReplyRunner(append_event_fn=lambda event, **payload: events.append((event, payload)))
        client = SequenceLlm(["这句已经发过了", "这次是新的回复"])

        result = runner._generate_guarded_reply(
            {"recent_auto_outbound_ttl_seconds": 3600},
            self._state_with_sent_reply(),
            client,
            "May",
            "新的入站消息",
            now=120,
            conversation_context=[],
            contact_memory={},
            screenshot_path=None,
            quoted_message=None,
        )

        self.assertEqual(result, "这次是新的回复")
        self.assertEqual(len(client.avoid_calls), 2)
        self.assertIn("这句已经发过了", client.avoid_calls[0])
        self.assertEqual([event for event, _ in events], ["duplicate_draft_detected", "duplicate_draft_regenerated"])

    def test_second_duplicate_is_blocked(self) -> None:
        events: list[tuple[str, dict]] = []
        runner = AutoReplyRunner(append_event_fn=lambda event, **payload: events.append((event, payload)))
        client = SequenceLlm(["这句已经发过了", "这句已经发过了"])

        result = runner._generate_guarded_reply(
            {"recent_auto_outbound_ttl_seconds": 3600},
            self._state_with_sent_reply(),
            client,
            "May",
            "另一条新消息",
            now=120,
            conversation_context=[],
            contact_memory={},
            screenshot_path=None,
            quoted_message=None,
        )

        self.assertEqual(result, "")
        self.assertEqual([event for event, _ in events], ["duplicate_draft_detected", "duplicate_draft_blocked"])

    def test_duplicate_retry_uses_latest_message_without_stale_context(self) -> None:
        runner = AutoReplyRunner(append_event_fn=lambda *args, **kwargs: None)
        client = SequenceLlm(["这句已经发过了", "新回复"])
        result = runner._generate_guarded_reply(
            {"recent_auto_outbound_ttl_seconds": 3600},
            self._state_with_sent_reply(),
            client,
            "May",
            "另一条新消息",
            now=120,
            conversation_context=[{"role": "self", "text": "过期话题"}],
            contact_memory={"recent_summary": "过期话题"},
            screenshot_path=None,
            quoted_message=None,
        )
        self.assertEqual(result, "新回复")
        self.assertEqual(client.context_calls[1], [])
        self.assertIsNone(client.memory_calls[1])

    def test_blocked_draft_stays_queued_until_new_draft_is_generated(self) -> None:
        class Idle:
            def get_idle_time_seconds(self) -> float:
                return 60.0

        saved: list[dict] = []
        events: list[str] = []
        client = SequenceLlm(["这句已经发过了", "这句已经发过了", "这次回新消息"])
        runner = AutoReplyRunner(
            idle_sensor=Idle(),
            llm_client=client,
            save_state_fn=lambda state: saved.append(dict(state)),
            append_event_fn=lambda event, **payload: events.append(event),
            now_fn=lambda: 120.0,
        )
        state = self._state_with_sent_reply()
        pending = {
            "contact": "May",
            "inbound_text": "另一条新消息",
            "draft_text": "",
            "due_at": 120.0,
            "inbound_fingerprint": "new-inbound",
        }
        state["pending_queue"] = [pending]

        result = runner._handle_pending(
            {"recent_auto_outbound_ttl_seconds": 3600, "send_delay_seconds": 300},
            state,
            pending,
            idle_seconds=60.0,
            now=120.0,
        )
        self.assertEqual(result["status"], "draft_retry_wait")
        self.assertEqual(state["pending_queue"][0]["draft_text"], "")
        self.assertEqual(state["pending_queue"][0]["due_at"], 240.0)

        result = runner._handle_pending(
            {"recent_auto_outbound_ttl_seconds": 3600, "send_delay_seconds": 300},
            state,
            pending,
            idle_seconds=60.0,
            now=240.0,
        )
        self.assertEqual(result["status"], "draft_retry_recovered")
        self.assertEqual(state["pending_queue"][0]["draft_text"], "这次回新消息")
        self.assertEqual(state["pending_queue"][0]["due_at"], 540.0)
        self.assertIn("draft_retry_recovered", events)


if __name__ == "__main__":
    unittest.main()
