from __future__ import annotations

import unittest

from wechat_autoreply.pending_queue import (
    prune_stale_pending,
    remove_pending_by_fingerprint,
    sync_pending_state,
)


class PendingQueueTests(unittest.TestCase):
    def test_sync_keeps_legacy_pending_mirror_consistent(self) -> None:
        queue = [{"contact": "May", "inbound_fingerprint": "a"}]
        state: dict = {"pending": None, "pending_queue": []}
        sync_pending_state(state, queue)
        self.assertIs(state["pending"], queue[0])
        self.assertIs(state["pending_queue"], queue)

    def test_remove_pending_uses_fingerprint_not_contact(self) -> None:
        first = {"contact": "Darren", "inbound_fingerprint": "one"}
        second = {"contact": "Darren", "inbound_fingerprint": "two"}
        self.assertEqual(remove_pending_by_fingerprint([first, second], first), [second])

    def test_stale_pending_uses_due_time_as_anchor(self) -> None:
        fresh = {"contact": "May", "created_at": 10, "due_at": 95}
        stale = {"contact": "Darren", "created_at": 10, "due_at": 20}
        kept, removed = prune_stale_pending([fresh, stale], now=100, ttl_seconds=50)
        self.assertEqual(kept, [fresh])
        self.assertEqual(removed, [stale])


if __name__ == "__main__":
    unittest.main()
