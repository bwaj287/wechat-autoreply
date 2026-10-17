from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def vote_text(values: list[str], *, allow_empty: bool = True) -> str:
    raws = [str(value or "").strip() for value in values]
    if not allow_empty:
        raws = [value for value in raws if value]
    if not raws:
        return ""
    normed = [normalize_text(value) for value in raws if allow_empty or value]
    if not normed:
        return ""
    winner_norm, _ = Counter(normed).most_common(1)[0]
    winner_raws = [value for value in raws if normalize_text(value) == winner_norm]
    return max(winner_raws, key=len) if winner_raws else ""


def vote_bool(values: list[bool]) -> bool:
    if not values:
        return False
    needed = (len(values) // 2) + 1
    return sum(1 for value in values if value) >= needed


@dataclass(frozen=True)
class RecheckConsensus:
    inbound: str
    raw_inbound: str
    outbound: str
    latest_outbound: bool
    anchor_snapshot: dict[str, Any]
    has_chat_window: bool
    frame_count: int


def build_recheck_consensus(samples: list[dict[str, Any]]) -> RecheckConsensus:
    if not samples:
        return RecheckConsensus("", "", "", False, {}, False, 0)

    voted_inbound = vote_text([str(item.get("inbound") or "") for item in samples])
    voted_raw_inbound = vote_text([str(item.get("raw_inbound") or "") for item in samples])
    voted_outbound = vote_text([str(item.get("outbound") or "") for item in samples])
    voted_latest_outbound = vote_bool([bool(item.get("latest_outbound")) for item in samples])

    anchor_snapshot = samples[-1]
    if voted_outbound:
        for item in reversed(samples):
            if normalize_text(str(item.get("outbound") or "")) == normalize_text(voted_outbound):
                anchor_snapshot = item
                break

    return RecheckConsensus(
        inbound=voted_inbound,
        raw_inbound=voted_raw_inbound,
        outbound=voted_outbound,
        latest_outbound=voted_latest_outbound,
        anchor_snapshot=anchor_snapshot,
        has_chat_window=any(bool(item.get("chat_window")) for item in samples),
        frame_count=len(samples),
    )
