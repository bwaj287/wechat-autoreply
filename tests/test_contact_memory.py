from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_autoreply import contact_memory


class ContactMemoryTests(unittest.TestCase):
    def test_context_messages_are_not_persisted_as_fresh_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_path = Path(tmp) / "contact_memory.json"
            seed_path = Path(tmp) / "missing_seed.json"
            with (
                patch.object(contact_memory, "CONTACT_MEMORY_PATH", runtime_path),
                patch.object(contact_memory, "CONTACT_MEMORY_SEED_PATH", seed_path),
            ):
                memory = contact_memory.remember_contact_memory(
                    "May",
                    context_messages=[
                        {"role": "contact", "text": "old joke that should not revive"},
                        {"role": "self", "text": "old reply that should not revive"},
                    ],
                    inbound_text="看了",
                    outbound_text="行",
                    now=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
                )

        event_texts = [str(item.get("text") or "") for item in memory["recent_events"]]
        self.assertEqual(event_texts, ["看了", "行"])
        self.assertNotIn("old joke", memory["recent_summary"])
        self.assertNotIn("old reply", memory["recent_summary"])

    def test_clear_all_recent_memory_preserves_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_path = Path(tmp) / "contact_memory.json"
            seed_path = Path(tmp) / "missing_seed.json"
            with (
                patch.object(contact_memory, "CONTACT_MEMORY_PATH", runtime_path),
                patch.object(contact_memory, "CONTACT_MEMORY_SEED_PATH", seed_path),
            ):
                contact_memory.set_contact_profile("May", "朋友，轻松聊天", locked=True)
                contact_memory.remember_contact_memory("May", inbound_text="吃好了", outbound_text="好")
                contact_memory.set_contact_profile("Darren", "朋友，可以开玩笑")
                contact_memory.remember_contact_memory("Darren", inbound_text="无了", outbound_text="无了？")

                result = contact_memory.clear_all_recent_memory()
                may = contact_memory.get_contact_memory("May")
                darren = contact_memory.get_contact_memory("Darren")

        self.assertEqual(result["cleared_count"], 2)
        self.assertEqual(may["profile"], "朋友，轻松聊天")
        self.assertTrue(may["profile_locked"])
        self.assertEqual(may["recent_summary"], "")
        self.assertEqual(may["recent_events"], [])
        self.assertEqual(darren["profile"], "朋友，可以开玩笑")
        self.assertEqual(darren["recent_summary"], "")
        self.assertEqual(darren["recent_events"], [])


if __name__ == "__main__":
    unittest.main()
