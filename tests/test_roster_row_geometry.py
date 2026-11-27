import unittest

from wechat_autoreply.wechat_ui import extract_visible_chats


def observation(text, left, top, width, height):
    return {
        "text": text,
        "bbox": {"x": left, "y": 1 - top - height, "w": width, "h": height, "left": left, "top": top},
        "center": {"y": top + height / 2},
    }


class RosterRowGeometryTests(unittest.TestCase):
    def test_body_and_adjacent_previews_cannot_become_the_bottom_contact(self):
        window = {"x": 0, "y": 30, "width": 1332, "height": 1320}
        items = [observation("已打开的其他聊天", .254, .017, .081, .015)]
        for i, label in enumerate(("联系人甲", "联系人乙", None, None, "目标联系人")):
            top = 0.723 + i * .055
            if label:
                items.append(observation(label, .092, top, .045, .013))
            items.append(observation(f"预览{i}", .093, top + .019, .11, .013))
            items.append(observation(f"聊天正文{i}", .290, top, .065, .014))
            if i != 3:
                stamp = "Yesterday 13:00" if i == 4 else "12:00"
                left = .144 if i == 4 else .192
                items.append(observation(stamp, left, top + .002, .218 - left, .009))
            items.append(observation("13:30", .306, top, .027, .009))
        items.append(observation("目标联系人", .297, .966, .08, .015))
        rows = extract_visible_chats(items, window)
        self.assertEqual([r["name"] for r in rows], ["联系人甲", "联系人乙", "目标联系人"])
        self.assertEqual([r["preview"] for r in rows], ["预览0", "预览1", "预览4"])
        self.assertEqual(rows[-1]["time"], "Yesterday 13:00")
        self.assertEqual(rows[-1]["click"], {"x": 153, "y": 1283})

    def test_missing_time_inside_regular_roster_still_uses_the_title_line(self):
        window = {"x": 0, "y": 0, "width": 1332, "height": 1320}
        items = []
        for i in range(5):
            top = .15 + i * .055
            items.append(observation(f"联系人{i}", .093, top, .05, .013))
            items.append(observation(f"摘要{i}", .093, top + .019, .09, .012))
            if i != 2:
                items.append(observation("09:30", .19, top + .002, .028, .009))
        rows = extract_visible_chats(items, window)
        self.assertEqual([r["name"] for r in rows], [f"联系人{i}" for i in range(5)])
        self.assertEqual(rows[2]["time"], "")
        self.assertEqual(rows[2]["preview"], "摘要2")
