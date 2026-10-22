from __future__ import annotations

import unittest

from wechat_autoreply.wechat_ui import _is_service_chat_name, extract_visible_chats


class ServiceRosterTests(unittest.TestCase):
    def test_service_name_matches_full_and_truncated_labels_only(self) -> None:
        for name in ("Service", "Service Notifications", "Servic...", "Servic…", "Serv..."):
            with self.subTest(name=name):
                self.assertTrue(_is_service_chat_name(name))
        self.assertFalse(_is_service_chat_name("Serviceable"))
        self.assertFalse(_is_service_chat_name("Servant..."))

    def test_service_row_is_not_available_to_click_paths(self) -> None:
        def item(name: str, top: float) -> dict:
            return {
                "text": name,
                "bbox": {"left": 0.15, "top": top, "x": 0.15, "y": 1 - top, "w": 0.10, "h": 0.03},
                "center": {"y": top + 0.015},
            }

        chats = extract_visible_chats(
            [item("Servic...", 0.2), item("May", 0.4)],
            {"x": 0, "y": 0, "width": 1000, "height": 800},
        )
        self.assertEqual([chat["name"] for chat in chats], ["May"])


if __name__ == "__main__":
    unittest.main()
