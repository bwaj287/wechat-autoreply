from __future__ import annotations

import unittest

from wechat_autoreply.manual_reply_policy import detect_manual_reply_trigger
from wechat_autoreply.orchestrator import _manual_reply_signal_is_reliable


class ManualReplyPolicyTests(unittest.TestCase):
    def test_outbound_after_inbound_is_manual_reply_evidence(self) -> None:
        result = detect_manual_reply_trigger(
            current_inbound="1000呢？",
            current_outbound="钱马上转过去",
            pending_outbound_snapshot="上一条",
            current_outbound_top=0.72,
            latest_inbound_top=0.60,
            latest_bubble_outbound=True,
            ultra_short_inbound_tail=False,
        )
        self.assertTrue(result.detected)
        self.assertEqual(result.trigger, "latest_bubble_outbound")

    def test_old_outbound_before_inbound_is_not_manual_reply(self) -> None:
        result = detect_manual_reply_trigger(
            current_inbound="1000呢？",
            current_outbound="上一条",
            pending_outbound_snapshot="上一条",
            current_outbound_top=0.42,
            latest_inbound_top=0.72,
            latest_bubble_outbound=False,
            ultra_short_inbound_tail=False,
        )
        self.assertFalse(result.detected)

    def test_ocr_missing_inbound_can_still_detect_changed_outbound(self) -> None:
        result = detect_manual_reply_trigger(
            current_inbound="",
            current_outbound="我手动回了",
            pending_outbound_snapshot="旧回复",
            current_outbound_top=0.72,
            latest_inbound_top=None,
            latest_bubble_outbound=True,
            ultra_short_inbound_tail=False,
        )
        self.assertEqual(result.trigger, "outbound_without_inbound")

    def test_missing_inbound_is_not_reliable_evidence_for_cancelling(self) -> None:
        reliable, blockers = _manual_reply_signal_is_reliable(
            current_outbound="你吃了啥",
            current_inbound="",
            current_outbound_item={"text": "你吃了啥", "left": 0.80, "width": 0.12, "top": 0.72},
            current_outbound_top=0.72,
            latest_inbound_top=None,
            latest_bubble_outbound=True,
            has_chat_window=True,
        )
        self.assertFalse(reliable)
        self.assertIn("no_inbound_text", blockers)


if __name__ == "__main__":
    unittest.main()
