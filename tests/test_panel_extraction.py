from __future__ import annotations

import copy
import unittest

from wechat_autoreply.wechat_ui import _extract_chat_panel, _extract_chat_window_panel, _panel_read_is_stable


def observation(text: str, top: float, left: float, width: float, role: str = "inbound") -> dict:
    return {
        "text": text,
        "bbox": {"top": top, "left": left, "w": width, "h": 0.03},
        "confidence": 0.9,
        "bubbleRole": role,
    }


class PanelExtractionTests(unittest.TestCase):
    def test_stable_panel_read_can_skip_third_frame(self) -> None:
        first = {"title": "May", "panel": {"inbound": [{"text": "到了", "confidence": 0.8}]}}
        second = {"title": "May", "panel": {"inbound": [{"text": "到了", "confidence": 0.9}]}}
        self.assertTrue(_panel_read_is_stable([first, second], "May"))
        self.assertFalse(_panel_read_is_stable([first], "May"))
        self.assertFalse(_panel_read_is_stable([first, {"title": "Ted Liu", "panel": second["panel"]}], "May"))
        self.assertFalse(_panel_read_is_stable([first, {"title": "May...", "panel": second["panel"]}], "May"))
        self.assertFalse(_panel_read_is_stable([first, {"title": "May", "panel": {"outbound": second["panel"]["inbound"]}}], "May"))
        self.assertFalse(_panel_read_is_stable([first, second], "Ted Liu"))
        self.assertFalse(_panel_read_is_stable([{"title": "May", "panel": {}}, {"title": "May", "panel": {}}], "May"))

    def test_each_window_keeps_its_calibrated_boundaries(self) -> None:
        # top, left, width, included in roster panel, included in standalone chat
        cases = [
            (0.109, 0.35, 0.20, False, False),
            (0.110, 0.35, 0.20, True, False),
            (0.120, 0.35, 0.20, True, True),
            (0.900, 0.35, 0.20, True, True),
            (0.910, 0.35, 0.20, False, True),
            (0.920, 0.35, 0.20, False, True),
            (0.921, 0.35, 0.20, False, False),
            (0.400, 0.29, 0.20, False, True),
            (0.400, 0.30, 0.20, True, True),
            (0.400, 0.35, 0.02, True, False),
            (0.400, 0.35, 0.025, True, True),
            (0.830, 0.35, 0.10, True, False),
            (0.830, 0.35, 0.15, True, True),
        ]
        for top, left, width, roster, standalone in cases:
            sample = [observation("message", top, left, width)]
            for extract, included in [(_extract_chat_panel, roster), (_extract_chat_window_panel, standalone)]:
                with self.subTest(extractor=extract.__name__, top=top, left=left, width=width):
                    panel = extract(sample)
                    self.assertEqual(bool(panel["inbound"] or panel["outbound"] or panel["misc"]), included)

    def test_both_windows_preserve_roles_metadata_and_noise_filtering(self) -> None:
        samples = [
            observation("incoming", 0.20, 0.32, 0.18),
            observation("outgoing", 0.50, 0.70, 0.18, "outbound"),
            observation("uncertain", 0.70, 0.45, 0.10, "unknown"),
            observation("Search", 0.30, 0.35, 0.20),
            observation("May", 0.40, 0.35, 0.20),
            observation("12:00", 0.60, 0.35, 0.20),
        ]
        original = copy.deepcopy(samples)
        for extract in [_extract_chat_panel, _extract_chat_window_panel]:
            with self.subTest(extractor=extract.__name__):
                panel = extract(samples, selected_title="May")
                self.assertEqual(panel["title"], "May")
                self.assertEqual(panel["latestInbound"], "incoming")
                self.assertEqual(panel["latestOutbound"], "outgoing")
                self.assertEqual([item["text"] for item in panel["misc"]], ["uncertain"])
                self.assertEqual(panel["inbound"][0]["confidence"], 0.9)
                self.assertEqual(samples, original)
