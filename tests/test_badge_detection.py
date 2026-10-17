from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from wechat_autoreply.badge_detection import fallback_row_badge_detection


class BadgeDetectionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
