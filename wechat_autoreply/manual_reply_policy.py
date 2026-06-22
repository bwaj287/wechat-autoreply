from __future__ import annotations

from dataclasses import dataclass


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


@dataclass(frozen=True)
class ManualReplyTrigger:
    trigger: str
    outbound_changed: bool
    outbound_after_inbound: bool
    has_inbound_evidence: bool

    @property
    def detected(self) -> bool:
        return bool(self.trigger)


def detect_manual_reply_trigger(
    *,
    current_inbound: str,
    current_outbound: str,
    pending_outbound_snapshot: str,
    current_outbound_top: float | None,
    latest_inbound_top: float | None,
    latest_bubble_outbound: bool,
    ultra_short_inbound_tail: bool,
) -> ManualReplyTrigger:
    outbound_changed = bool(
        current_outbound
        and normalize_text(current_outbound) != normalize_text(pending_outbound_snapshot)
    )
    outbound_after_inbound = bool(
        current_outbound
        and current_outbound_top is not None
        and latest_inbound_top is not None
        and current_outbound_top > latest_inbound_top + 0.01
    )
    has_inbound_evidence = bool(current_inbound) or latest_inbound_top is not None

    trigger = ""
    if not current_inbound and outbound_changed:
        trigger = "outbound_without_inbound"
    elif latest_bubble_outbound and has_inbound_evidence and current_outbound:
        trigger = "latest_bubble_outbound"
    elif outbound_changed and has_inbound_evidence and outbound_after_inbound:
        trigger = "outbound_after_latest_inbound"
    elif outbound_changed and ultra_short_inbound_tail:
        trigger = "outbound_with_ultra_short_inbound_tail"

    return ManualReplyTrigger(
        trigger=trigger,
        outbound_changed=outbound_changed,
        outbound_after_inbound=outbound_after_inbound,
        has_inbound_evidence=has_inbound_evidence,
    )
