from __future__ import annotations

import unittest

from wechat_autoreply.outbound_history import (
    draft_match_mode,
    match_recent_auto_outbound,
    recent_auto_outbound_texts,
    remember_recent_auto_outbound,
)


class OutboundHistoryTests(unittest.TestCase):
    def test_recent_texts_return_newest_unique_replies(self) -> None:
        state: dict = {}
        for now, text in ((10, "第一条"), (20, "第二条"), (30, "第三条")):
            remember_recent_auto_outbound(
                state,
                "May",
                text,
                now=now,
                source="auto_sent",
                ttl_seconds=3600,
            )

        self.assertEqual(
            recent_auto_outbound_texts(state, "May", now=40, ttl_seconds=3600, limit=2),
            ["第二条", "第三条"],
        )

    def test_send_confirmation_ignores_punctuation_and_emoji_codes(self) -> None:
        self.assertIn(
            draft_match_mode("钱马上转过去", "钱马上转过去！[旺柴]"),
            {"normalized_substring", "canonical_exact"},
        )

    def test_truncated_tail_matches_recent_auto_outbound(self) -> None:
        state: dict = {}
        remember_recent_auto_outbound(
            state,
            "王哥",
            "这也能整出来？别吓唬我，最近身体咋样，实习安排得还顺利不？",
            now=100,
            source="send_attempt",
            ttl_seconds=3600,
        )
        match = match_recent_auto_outbound(
            state,
            "王哥",
            "实习安排得还顺利不？",
            now=130,
            ttl_seconds=3600,
        )
        self.assertEqual(match["match_mode"], "tail_fragment")

    def test_expired_outbound_is_not_used_as_self_echo(self) -> None:
        state: dict = {}
        remember_recent_auto_outbound(
            state,
            "Darren",
            "无了？",
            now=10,
            source="send_attempt",
            ttl_seconds=20,
        )
        self.assertEqual(
            match_recent_auto_outbound(
                state,
                "Darren",
                "无了？",
                now=40,
                ttl_seconds=20,
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
