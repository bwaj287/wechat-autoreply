from __future__ import annotations

import unittest

from wechat_autoreply.recheck_policy import build_recheck_consensus


class RecheckPolicyTests(unittest.TestCase):
    def test_majority_vote_suppresses_single_stale_ocr_frame(self) -> None:
        samples = [
            {"inbound": "无了", "raw_inbound": "无了", "outbound": "无了？", "latest_outbound": False},
            {"inbound": "无了", "raw_inbound": "无了", "outbound": "无了？", "latest_outbound": False},
            {"inbound": "费了", "raw_inbound": "费了", "outbound": "", "latest_outbound": True},
        ]
        result = build_recheck_consensus(samples)
        self.assertEqual(result.inbound, "无了")
        self.assertEqual(result.outbound, "无了？")
        self.assertFalse(result.latest_outbound)
        self.assertEqual(result.frame_count, 3)

    def test_anchor_follows_voted_outbound_instead_of_last_frame(self) -> None:
        expected_anchor = {
            "outbound": "确认发送",
            "outbound_top": 0.72,
            "panel": {"latestOutbound": "确认发送"},
        }
        samples = [
            expected_anchor,
            {"outbound": "确认发送", "outbound_top": 0.71, "panel": {}},
            {"outbound": "旧消息", "outbound_top": 0.50, "panel": {}},
        ]
        result = build_recheck_consensus(samples)
        self.assertEqual(result.outbound, "确认发送")
        self.assertEqual(result.anchor_snapshot["outbound_top"], 0.71)


if __name__ == "__main__":
    unittest.main()
