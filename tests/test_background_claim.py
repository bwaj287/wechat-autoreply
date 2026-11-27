import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wechat_autoreply.background_claim import process_background_claims
from wechat_autoreply.orchestrator import AutoReplyRunner


def roster(preview="下午去取快递", timestamp="13:01", unread=True):
    return {"status": "ok", "visibleChats": [{
        "name": "小林", "preview": preview, "time": timestamp, "numericBadge": unread,
        "unread": unread, "digitPixelCount": 14 if unread else 0, "redPixelCount": 40,
    }]}


class BackgroundClaimTests(unittest.TestCase):
    def setUp(self):
        self.roster = roster()
        self.panel = {
            "title": "小林", "latestInbound": "下午去取快递",
            "inbound": [{"text": "下午去取快递", "top": .69, "left": .25, "width": .16, "grayPixels": 100}],
            "outbound": [], "misc": [],
        }
        self.read = Mock(return_value={
            **copy.deepcopy(self.roster), "selectionConfirmed": True, "activeChat": "小林",
            "chatPanel": self.panel,
        })
        self.llm = Mock()
        self.llm.generate_reply.return_value = "好，知道了"
        self.runner = AutoReplyRunner(
            ui=SimpleNamespace(probe_chat_background=self.read), llm_client=self.llm,
            idle_sensor=Mock(), save_state_fn=Mock(), append_event_fn=Mock(),
        )
        self.runner._live_idle_seconds = lambda: 0
        self.config = {"allowed_contacts": ["小林"], "contact_memory_enabled": False, "send_delay_seconds": 180}
        self.state = {}

    def observe(self, snapshot, now=11):
        return process_background_claims(self.runner, self.config, self.state, snapshot, now)

    def baseline(self):
        self.observe(roster("明天再聊", "12:59", False), now=10)

    def test_verified_transition_generates_one_draft_while_user_is_active(self):
        self.baseline()
        self.assertEqual(self.observe(self.roster)["draft_count"], 1)
        self.assertEqual(self.observe(self.roster)["draft_count"], 0)
        self.assertEqual(self.state["pending_queue"], [self.state["pending"]])
        self.assertEqual(self.state["pending"]["draft_text"], "好，知道了")
        self.assertEqual(self.state["pending"]["due_at"], 191)
        self.assertFalse(self.state["background_claim_opportunities"])
        self.llm.generate_reply.assert_called_once()

    def test_cached_badge_alone_never_claims_and_disappearing_badge_preserves_opportunity(self):
        self.observe(self.roster)
        self.observe(self.roster)
        self.observe(roster(unread=False))
        self.assertIn("小林", self.state["background_claim_opportunities"])
        self.assertFalse(self.state["pending_queue"])
        self.read.assert_not_called()

    def test_old_baseline_and_remarking_an_unchanged_message_only_defer(self):
        for first, now in ((roster(unread=False), 11), (roster("旧消息", "12:59", False), 200)):
            with self.subTest(now=now):
                self.state = {}
                self.observe(first, now=10)
                self.observe(self.roster, now=now)
                self.assertIn("小林", self.state["background_claim_opportunities"])
                self.read.assert_not_called()

    def test_ambiguous_rows_keep_foreground_scan_without_guessing_a_chat(self):
        self.baseline()
        self.observe({"status": "ok", "visibleChats": self.roster["visibleChats"] * 2})
        self.assertTrue(self.state["foreground_scan_pending"])
        self.assertFalse(self.state["background_claim_opportunities"])
        self.read.assert_not_called()

    def test_other_contact_and_non_numeric_indicator_never_claim(self):
        for row in [dict(self.roster["visibleChats"][0], name="其他人"), dict(self.roster["visibleChats"][0], numericBadge=False)]:
            with self.subTest(row=row):
                self.baseline()
                self.observe({"status": "ok", "visibleChats": [row]})
                self.assertFalse(self.state["background_claim_opportunities"])
                self.read.assert_not_called()

    def test_wrong_title_changed_second_row_or_manual_reply_preserves_existing_queue(self):
        original = {"contact": "小林", "inbound_text": "还没决定", "inbound_fingerprint": "old", "draft_text": "好的"}
        for reason in ("other_title", "different_preview", "different_time", "outbound", "unknown_sender", "unreadable"):
            with self.subTest(reason=reason):
                self.state = {"pending_queue": [copy.deepcopy(original)]}
                self.baseline()
                selected = {**copy.deepcopy(self.roster), "status": "ok", "selectionConfirmed": True,
                            "activeChat": "小林", "chatPanel": copy.deepcopy(self.panel)}
                if reason == "other_title":
                    selected["activeChat"] = "同事"
                elif reason == "different_preview":
                    selected["visibleChats"][0]["preview"] = "变了"
                elif reason == "different_time":
                    selected["visibleChats"][0]["time"] = "13:06"
                elif reason == "outbound":
                    selected["chatPanel"]["outbound"] = [{"text": "我自己回了", "top": .84, "left": .7}]
                elif reason == "unknown_sender":
                    selected["chatPanel"]["misc"] = [{"text": "看不清是谁", "top": .84}]
                else:
                    self.read.side_effect = RuntimeError("capture failed")
                self.read.return_value = selected
                self.observe(self.roster)
                self.assertEqual(self.state["pending_queue"], [original])
                self.assertIn("小林", self.state["background_claim_opportunities"])
                self.llm.generate_reply.assert_not_called()
