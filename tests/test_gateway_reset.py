from __future__ import annotations

import unittest

from apps.gateway.cli import _fresh_runtime_state_preserving_safety_history


class GatewayResetTests(unittest.TestCase):
    def test_reset_preserves_recent_outbound_safety_history_only(self) -> None:
        before = {
            "pending_queue": [{"contact": "May"}],
            "pending": {"contact": "May"},
            "recent_auto_outbounds": {
                "May": [{"text": "刚发过", "ts": 100, "source": "auto_sent"}],
            },
        }

        state = _fresh_runtime_state_preserving_safety_history(before)

        self.assertEqual(state["pending_queue"], [])
        self.assertIsNone(state["pending"])
        self.assertEqual(state["recent_auto_outbounds"], before["recent_auto_outbounds"])
        self.assertIsNot(state["recent_auto_outbounds"], before["recent_auto_outbounds"])


if __name__ == "__main__":
    unittest.main()
