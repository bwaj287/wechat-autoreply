from __future__ import annotations

import unittest

from wechat_autoreply.ollama_client import _format_avoid_replies_block, _format_context_block
from wechat_autoreply.orchestrator import build_reply_context


class ReplyContextTests(unittest.TestCase):
    def test_avoid_block_marks_already_sent_replies(self) -> None:
        block = _format_avoid_replies_block(["刚发过的回复"])

        self.assertIn("[ALREADY_SENT_DO_NOT_REPEAT] 刚发过的回复", block)
        self.assertIn("genuinely new reply", block)

    def test_context_preserves_sender_roles_in_screen_order(self) -> None:
        panel = {
            "inbound": [
                {"text": "你去不去", "top": 0.30},
                {"text": "那我先走了", "top": 0.70},
            ],
            "outbound": [
                {"text": "等我一下", "top": 0.50},
            ],
        }

        context = build_reply_context(panel, "那我先走了", max_messages=8)

        self.assertEqual(
            context,
            [
                {"role": "contact", "text": "你去不去"},
                {"role": "self", "text": "等我一下"},
                {"role": "contact", "text": "那我先走了"},
            ],
        )

    def test_formatted_context_makes_own_messages_unambiguous(self) -> None:
        block = _format_context_block(
            [
                {"role": "contact", "text": "你去不去"},
                {"role": "self", "text": "等我一下"},
            ]
        )

        self.assertIn("[CONTACT_SENT] 你去不去", block)
        self.assertIn("[YOU_SENT] 等我一下", block)
        self.assertNotIn("Me:", block)
        self.assertNotIn("Them:", block)

    def test_unknown_role_is_not_treated_as_contact(self) -> None:
        block = _format_context_block(
            [
                {"role": "unknown", "text": "不确定是谁发的"},
                {"role": "contact", "text": "明确是对方发的"},
            ]
        )

        self.assertNotIn("不确定是谁发的", block)
        self.assertIn("[CONTACT_SENT] 明确是对方发的", block)


if __name__ == "__main__":
    unittest.main()
