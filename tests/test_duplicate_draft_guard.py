from __future__ import annotations

import unittest

from wechat_autoreply.orchestrator import AutoReplyRunner
from wechat_autoreply.outbound_history import remember_recent_auto_outbound


class SequenceLlm:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.avoid_calls: list[list[str]] = []

    def generate_reply(self, contact: str, inbound_text: str, *, avoid_replies=None, **kwargs) -> str:
        self.avoid_calls.append(list(avoid_replies or []))
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


if __name__ == "__main__":
    unittest.main()
