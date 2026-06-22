from __future__ import annotations

import unittest

from wechat_autoreply.claim_policy import (
    CLAIM_TRIGGER_MENU,
    CLAIM_TRIGGER_PASSIVE,
    decide_claim,
    evaluate_passive_preflight,
    has_row_numeric_unread_badge,
    menu_signal_rising,
)


class ClaimPolicyTests(unittest.TestCase):
    def test_passive_sweep_without_badge_stays_closed(self) -> None:
        decision = decide_claim(
            idle_seconds=45,
            idle_threshold=30,
            has_queue=False,
            pending_due=False,
            sweep_while_pending=False,
            actionable_menu_signal=False,
            menu_checked_now=True,
            menu_signal_rising_now=False,
            roster_sweep_due=True,
            claim_retry_pending=False,
            passive_claim_ready=False,
        )
        self.assertFalse(decision.should_claim)
        self.assertEqual(decision.reason, "no_claim_evidence")

    def test_passive_badge_opens_claim_flow(self) -> None:
        decision = decide_claim(
            idle_seconds=45,
            idle_threshold=30,
            has_queue=False,
            pending_due=False,
            sweep_while_pending=False,
            actionable_menu_signal=False,
            menu_checked_now=True,
            menu_signal_rising_now=False,
            roster_sweep_due=True,
            claim_retry_pending=False,
            passive_claim_ready=True,
        )
        self.assertTrue(decision.should_claim)
        self.assertEqual(decision.trigger, CLAIM_TRIGGER_PASSIVE)

    def test_new_menu_signal_can_interrupt_pending_delay(self) -> None:
        decision = decide_claim(
            idle_seconds=45,
            idle_threshold=30,
            has_queue=True,
            pending_due=False,
            sweep_while_pending=False,
            actionable_menu_signal=True,
            menu_checked_now=True,
            menu_signal_rising_now=True,
            roster_sweep_due=False,
            claim_retry_pending=False,
            passive_claim_ready=False,
        )
        self.assertTrue(decision.should_claim)
        self.assertTrue(decision.pending_allowed)
        self.assertEqual(decision.trigger, CLAIM_TRIGGER_MENU)

    def test_passive_badge_does_not_interrupt_pending_delay(self) -> None:
        decision = decide_claim(
            idle_seconds=45,
            idle_threshold=30,
            has_queue=True,
            pending_due=False,
            sweep_while_pending=False,
            actionable_menu_signal=False,
            menu_checked_now=True,
            menu_signal_rising_now=False,
            roster_sweep_due=True,
            claim_retry_pending=False,
            passive_claim_ready=True,
        )
        self.assertFalse(decision.should_claim)
        self.assertEqual(decision.reason, "pending_queue_blocks_claim")

    def test_preflight_only_accepts_whitelist_numeric_badges(self) -> None:
        visible = [
            {
                "name": "Darren",
                "numericBadge": True,
                "digitPixelCount": 10,
                "redPixelCount": 179,
            },
            {
                "name": "工作群",
                "numericBadge": True,
                "digitPixelCount": 10,
                "redPixelCount": 120,
            },
        ]
        result = evaluate_passive_preflight(visible, ["Darren"])
        self.assertTrue(result.badge_detected)
        self.assertEqual([row["name"] for row in result.badge_rows], ["Darren"])

    def test_plain_red_avatar_is_not_numeric_badge(self) -> None:
        self.assertFalse(
            has_row_numeric_unread_badge(
                {
                    "numericBadge": False,
                    "digitPixelCount": 30,
                    "redPixelCount": 400,
                }
            )
        )

    def test_menu_signal_rising_requires_new_or_larger_count(self) -> None:
        self.assertTrue(menu_signal_rising("1", ""))
        self.assertTrue(menu_signal_rising("2", "1"))
        self.assertFalse(menu_signal_rising("1", "1"))
        self.assertFalse(menu_signal_rising("", "1"))

    def test_same_menu_signal_does_not_reopen_on_sweep_due(self) -> None:
        decision = decide_claim(
            idle_seconds=45,
            idle_threshold=30,
            has_queue=False,
            pending_due=False,
            sweep_while_pending=False,
            actionable_menu_signal=True,
            menu_checked_now=False,
            menu_signal_rising_now=False,
            roster_sweep_due=True,
            claim_retry_pending=False,
            passive_claim_ready=False,
        )
        self.assertFalse(decision.should_claim)
        self.assertEqual(decision.reason, "no_claim_evidence")


if __name__ == "__main__":
    unittest.main()
