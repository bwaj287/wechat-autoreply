from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from wechat_autoreply.badge_detection import fallback_row_badge_detection


class BadgeDetectionTests(unittest.TestCase):
    def test_previous_row_preview_emoji_is_not_next_contacts_badge(self) -> None:
        row = {"rowTop": 0.3518, "rowBottom": 0.4291, "nameTop": 0.3909, "nameLeft": 0.1047}
        image = Image.new("RGB", (1182, 945), (36, 36, 36))
        draw = ImageDraw.Draw(image)
        draw.ellipse((126, 325, 141, 335), fill=(245, 100, 35))
        draw.rectangle((131, 328, 136, 331), fill="white")
        self.assertIsNone(fallback_row_badge_detection(image, row))

    def test_orange_name_icon_is_not_an_unread_badge(self) -> None:
        image = Image.new("RGB", (1182, 945), (36, 36, 36))
        draw = ImageDraw.Draw(image)
        draw.ellipse((125, 731, 144, 748), fill=(245, 150, 30))
        draw.rectangle((131, 736, 137, 743), fill="white")
        # OCR can include the icon in the name, or start after it.
        for name_left in (0.1074, 0.1203):
            with self.subTest(name_left=name_left):
                row = {"rowTop": 0.7375, "rowBottom": 0.8147, "nameTop": 0.7756, "nameLeft": name_left}
                self.assertIsNone(fallback_row_badge_detection(image, row))

    def test_real_badge_survives_neighboring_emoji_and_ocr_jitter(self) -> None:
        for offset in (-3, 0, 3):
            with self.subTest(offset=offset):
                row = {"rowTop": 0.3518, "rowBottom": 0.4291, "nameTop": (369 + offset) / 945, "nameLeft": 0.1047}
                image = Image.new("RGB", (1182, 945), (36, 36, 36))
                draw = ImageDraw.Draw(image)
                draw.ellipse((126, 325, 141, 335), fill=(245, 100, 35))
                draw.rectangle((131, 328, 136, 331), fill="white")
                draw.ellipse((106, 358, 123, 375), fill=(250, 55, 50))
                draw.line((114, 363, 114, 370), fill="white", width=2)
                detected = fallback_row_badge_detection(image, row)
                self.assertIsNotNone(detected)
                self.assertTrue(detected["numericBadge"])

    def test_preview_emoji_below_name_does_not_look_like_unread_badge(self) -> None:
        row = {"rowTop": 0.128, "rowBottom": 0.212, "nameTop": 0.1696, "nameLeft": 0.1032}
        image = Image.new("RGB", (1180, 890), (36, 36, 36))
        draw = ImageDraw.Draw(image)
        draw.ellipse((118, 178, 135, 195), fill=(245, 57, 50))
        draw.line((125, 183, 125, 190), fill=(255, 255, 255), width=2)
        self.assertIsNone(fallback_row_badge_detection(image, row))

        draw.ellipse((113, 143, 130, 160), fill=(245, 57, 50))
        draw.line((121, 148, 121, 155), fill=(255, 255, 255), width=2)
        self.assertIsNotNone(fallback_row_badge_detection(image, row))

    def test_attached_badge_is_recovered_but_warm_avatar_alone_is_not(self) -> None:
        row = {"rowTop": 0.0356, "rowBottom": 0.1631, "nameLeft": 0.1063}

        image = Image.new("RGB", (1148, 735), (36, 36, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle((90, 82, 115, 112), fill=(180, 45, 20))
        draw.ellipse((106, 71, 122, 87), fill=(250, 55, 50))
        draw.line((114, 76, 114, 82), fill=(255, 255, 255), width=2)
        detected = fallback_row_badge_detection(image, row)
        self.assertIsNotNone(detected)
        self.assertTrue(detected["numericBadge"])

        avatar_only = Image.new("RGB", (1148, 735), (36, 36, 36))
        avatar_draw = ImageDraw.Draw(avatar_only)
        avatar_draw.rectangle((90, 82, 115, 112), fill=(180, 45, 20))
        avatar_draw.line((97, 90, 98, 96), fill=(255, 255, 255), width=2)
        self.assertIsNone(fallback_row_badge_detection(avatar_only, row))

        row["nameTop"] = 82 / 735
        self.assertIsNotNone(fallback_row_badge_detection(image, row))
        row["nameTop"] = 120 / 735
        self.assertIsNone(fallback_row_badge_detection(image, row))


if __name__ == "__main__":
    unittest.main()
