from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from selftest import FakeIdle, FakeUI, MemoryStore
from wechat_autoreply import wechat_ui
from wechat_autoreply.orchestrator import AutoReplyRunner, extract_inbound_payload, fingerprint, latest_bubble_is_unresolved


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swift"), "requires Apple image APIs")
class BubbleCoordinatesIntegrationTests(unittest.TestCase):
    def test_bottom_green_bubble_cancels_pending_through_real_color_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bubbles.png"
            image = Image.new("RGB", (1000, 800), (25, 25, 25))
            draw = ImageDraw.Draw(image)
            draw.rectangle((310, 400, 560, 455), fill=(48, 48, 48))
            draw.rectangle((840, 630, 940, 680), fill=(48, 195, 127))
            image.save(path)
            items = []
            for text, left, top, width, height in (
                ("incoming message", .33, .52, .20, .025),
                ("fuel", .86, .81, .06, .025),
            ):
                items.append({"text": text, "confidence": 1.0,
                              "bbox": {"x": left, "y": 1 - top - height, "w": width,
                                       "h": height, "left": left, "top": top}})
            wechat_ui._annotate_bubble_roles(items, path)
            self.assertEqual([o["bubbleRole"] for o in items], ["inbound", "outbound"])
            panel = wechat_ui._extract_chat_panel(items, selected_title="May")
            self.assertEqual(panel["latestOutbound"], "fuel")
            store = MemoryStore()
            store.config.update(contact_memory_enabled=False, recheck_vote_frames=1)
            pending = {"contact": "May", "inbound_text": "incoming message", "draft_text": "old draft",
                       "inbound_fingerprint": fingerprint("May", "incoming message"),
                       "created_at": 800, "due_at": 900, "send_attempts": 0}
            store.state["pending_queue"] = [pending]
            ui = FakeUI([{"status": "ok", "selectionConfirmed": True, "activeChat": "May",
                          "chatPanel": panel}])
            runner = AutoReplyRunner(ui=ui, idle_sensor=FakeIdle(60), now_fn=lambda: 1000,
                                     append_event_fn=store.append_event, save_state_fn=store.save_state)
            with patch.object(runner, "_send_message_ui") as send:
                result = runner._handle_pending(store.config, store.state, pending, 60, 1000)
            self.assertEqual(result["reason"], "manual_reply_detected")
            send.assert_not_called()
            self.assertEqual(store.state["pending_queue"], [])


class QuotedTailTests(unittest.TestCase):
    def test_unknown_latest_bubble_defers_send_but_not_old_unknown_text(self) -> None:
        panel = {"inbound": [{"text": "incoming message", "top": .6, "confidence": 1}],
                 "latestInbound": "incoming message", "misc": [{"text": "fuel", "top": .81}]}
        self.assertTrue(latest_bubble_is_unresolved(panel))
        self.assertFalse(latest_bubble_is_unresolved({**panel, "misc": [{"text": "old", "top": .4}]}))
        store = MemoryStore()
        store.config.update(contact_memory_enabled=False, recheck_vote_frames=1)
        pending = {"contact": "May", "inbound_text": "incoming message", "draft_text": "old draft",
                   "inbound_fingerprint": fingerprint("May", "incoming message"),
                   "created_at": 800, "due_at": 900, "send_attempts": 0}
        store.state["pending_queue"] = [pending]
        ui = FakeUI([{"status": "ok", "selectionConfirmed": True, "activeChat": "May", "chatPanel": panel}])
        runner = AutoReplyRunner(ui=ui, idle_sensor=FakeIdle(60), now_fn=lambda: 1000,
                                 append_event_fn=store.append_event, save_state_fn=store.save_state)
        with patch.object(runner, "_send_message_ui") as send:
            result = runner._handle_pending(store.config, store.state, pending, 60, 1000)
        self.assertEqual(result["reason"], "unresolved_latest_bubble")
        send.assert_not_called()
        self.assertEqual(len(store.state["pending_queue"]), 1)

    def test_message_after_quote_is_not_discarded(self) -> None:
        panel = {
            "outbound": [{"text": "paid leave?", "top": .4}],
            "latestOutbound": "paid leave?",
            "inbound": [
                {"text": "yes paid", "top": .60},
                {"text": "Me: paid leave?", "top": .64},
                {"text": "they called for help", "top": .69},
            ],
        }
        payload = extract_inbound_payload(panel)
        self.assertIn("they called for help", payload["text"])
        self.assertNotIn("Me:", payload["text"])


if __name__ == "__main__":
    unittest.main()
