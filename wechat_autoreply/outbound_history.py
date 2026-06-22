from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import re
from typing import Any

from . import wechat_ui


EMOJI_CODE_RE = re.compile(r"\[[^\[\]\s]{1,12}\]")
EMOJI_CHAR_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
RECENT_AUTO_OUTBOUND_LIMIT = 8
RECENT_AUTO_OUTBOUND_TTL_SECONDS = 6 * 60 * 60


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def compact_message_text(text: str) -> str:
    value = normalize_text(text)
    return re.sub(r"[\s…。,，!！?？~～·•`'\"\\-_/\\\\|]+", "", value)


def messages_overlap(first: str, second: str) -> bool:
    first_norm = normalize_text(first)
    second_norm = normalize_text(second)
    if not first_norm or not second_norm:
        return False
    if first_norm == second_norm or first_norm in second_norm or second_norm in first_norm:
        return True
    first_compact = compact_message_text(first_norm)
    second_compact = compact_message_text(second_norm)
    if not first_compact or not second_compact:
        return False
    return first_compact in second_compact or second_compact in first_compact


def message_similarity_score(first: str, second: str) -> float:
    first_compact = compact_message_text(first)
    second_compact = compact_message_text(second)
    if not first_compact or not second_compact:
        return 0.0
    if first_compact == second_compact:
        return 1.0
    if first_compact in second_compact or second_compact in first_compact:
        shorter = min(len(first_compact), len(second_compact))
        longer = max(len(first_compact), len(second_compact))
        if longer and (shorter / longer) >= 0.72:
            return 0.96
    if len(first_compact) == len(second_compact) and len(first_compact) >= 4:
        mismatch = sum(1 for left, right in zip(first_compact, second_compact) if left != right)
        if mismatch <= 1:
            return 0.97
    return SequenceMatcher(None, first_compact, second_compact).ratio()


def canonical_reply_text(text: str) -> str:
    value = normalize_text(text)
    if not value:
        return ""
    value = EMOJI_CODE_RE.sub("", value)
    value = EMOJI_CHAR_RE.sub("", value)
    return re.sub(r"[\s…。,，!！?？~～·•`'\"\\\-_/\\\\|:：;；\[\]\(\)（）【】<>{}《》]+", "", value)


def draft_match_mode(draft_text: str, outbound_text: str) -> str:
    draft_norm = normalize_text(draft_text)
    outbound_norm = normalize_text(outbound_text)
    if not draft_norm or not outbound_norm:
        return ""
    if draft_norm == outbound_norm:
        return "strict"
    if draft_norm in outbound_norm or outbound_norm in draft_norm:
        return "normalized_substring"

    draft_canonical = canonical_reply_text(draft_norm)
    outbound_canonical = canonical_reply_text(outbound_norm)
    if not draft_canonical or not outbound_canonical:
        return ""
    if draft_canonical == outbound_canonical:
        return "canonical_exact"
    if draft_canonical in outbound_canonical or outbound_canonical in draft_canonical:
        return "canonical_substring"

    max_len = max(len(draft_canonical), len(outbound_canonical))
    if max_len < 6:
        return ""
    min_len = min(len(draft_canonical), len(outbound_canonical))
    if min_len < max(3, int(max_len * 0.55)):
        return ""
    overlap = sum((Counter(draft_canonical) & Counter(outbound_canonical)).values())
    similarity = overlap / max_len if max_len else 0.0
    return "canonical_charbag" if similarity >= 0.82 else ""


def auto_outbound_echo_match_mode(candidate_text: str, reference_text: str) -> str:
    candidate = str(candidate_text or "").strip()
    reference = str(reference_text or "").strip()
    if not candidate or not reference:
        return ""
    candidate_canonical = canonical_reply_text(candidate)
    reference_canonical = canonical_reply_text(reference)
    if not candidate_canonical or not reference_canonical:
        return ""
    if candidate_canonical == reference_canonical:
        return "canonical_exact"
    if (
        4 <= len(candidate_canonical) <= 16
        and len(reference_canonical) > len(candidate_canonical)
        and reference_canonical.endswith(candidate_canonical)
    ):
        return "tail_fragment"
    if (
        len(candidate_canonical) >= 6
        and len(reference_canonical) > len(candidate_canonical)
        and reference_canonical.startswith(candidate_canonical)
    ):
        return "head_fragment"
    if (
        min(len(candidate_canonical), len(reference_canonical)) >= 6
        and (candidate_canonical in reference_canonical or reference_canonical in candidate_canonical)
    ):
        return "canonical_substring"
    if min(len(candidate_canonical), len(reference_canonical)) >= 8:
        if message_similarity_score(candidate, reference) >= 0.88:
            return "similarity"
    return ""


def recent_auto_outbound_ttl(config: dict[str, Any]) -> float:
    try:
        value = float(config.get("recent_auto_outbound_ttl_seconds", RECENT_AUTO_OUTBOUND_TTL_SECONDS))
    except Exception:
        value = RECENT_AUTO_OUTBOUND_TTL_SECONDS
    return max(0.0, value)


def _recent_auto_outbounds_bucket(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    bucket = state.get("recent_auto_outbounds")
    if not isinstance(bucket, dict):
        bucket = {}
        state["recent_auto_outbounds"] = bucket
    return bucket


def prune_recent_auto_outbounds(
    state: dict[str, Any],
    *,
    now: float,
    ttl_seconds: float,
    limit: int = RECENT_AUTO_OUTBOUND_LIMIT,
) -> None:
    bucket = _recent_auto_outbounds_bucket(state)
    if ttl_seconds <= 0:
        bucket.clear()
        return
    for contact_key in list(bucket.keys()):
        raw_entries = bucket.get(contact_key)
        if not isinstance(raw_entries, list):
            bucket.pop(contact_key, None)
            continue
        kept: list[dict[str, Any]] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            text = str(raw_entry.get("text") or "").strip()
            if not text:
                continue
            ts = float(raw_entry.get("ts", 0.0) or 0.0)
            if ts > 0 and now - ts > ttl_seconds:
                continue
            kept.append(
                {
                    "text": text,
                    "ts": ts,
                    "source": str(raw_entry.get("source") or "").strip(),
                }
            )
        if kept:
            bucket[contact_key] = kept[-limit:]
        else:
            bucket.pop(contact_key, None)


def _recent_auto_outbound_entries(
    state: dict[str, Any],
    contact: str,
    *,
    now: float,
    ttl_seconds: float,
) -> list[dict[str, Any]]:
    prune_recent_auto_outbounds(state, now=now, ttl_seconds=ttl_seconds)
    bucket = _recent_auto_outbounds_bucket(state)
    contact_value = str(contact or "").strip()
    entries = list(bucket.get(contact_value) or [])
    if entries:
        return entries
    for key, value in bucket.items():
        if wechat_ui.names_match(str(key), contact_value):
            return list(value or [])
    return []


def remember_recent_auto_outbound(
    state: dict[str, Any],
    contact: str,
    text: str,
    *,
    now: float,
    source: str,
    ttl_seconds: float,
    limit: int = RECENT_AUTO_OUTBOUND_LIMIT,
) -> dict[str, Any]:
    value = str(text or "").strip()
    contact_value = str(contact or "").strip()
    if not contact_value or not wechat_ui.has_meaningful_text(value):
        return {}
    prune_recent_auto_outbounds(state, now=now, ttl_seconds=ttl_seconds, limit=limit)
    bucket = _recent_auto_outbounds_bucket(state)
    entries = list(bucket.get(contact_value) or [])
    normalized_value = normalize_text(value)
    entries = [entry for entry in entries if normalize_text(str(entry.get("text") or "")) != normalized_value]
    entry = {"text": value, "ts": now, "source": str(source or "").strip()}
    entries.append(entry)
    bucket[contact_value] = entries[-limit:]
    return entry


def match_recent_auto_outbound(
    state: dict[str, Any],
    contact: str,
    text: str,
    *,
    now: float,
    ttl_seconds: float,
    extra_texts: list[str] | None = None,
) -> dict[str, Any]:
    value = str(text or "").strip()
    if not value:
        return {}
    entries = _recent_auto_outbound_entries(state, contact, now=now, ttl_seconds=ttl_seconds)
    for extra_text in list(extra_texts or []):
        extra_value = str(extra_text or "").strip()
        if extra_value:
            entries.append({"text": extra_value, "ts": now, "source": "current_pending_draft"})
    seen: set[str] = set()
    for entry in reversed(entries):
        reference = str(entry.get("text") or "").strip()
        key = normalize_text(reference)
        if not reference or key in seen:
            continue
        seen.add(key)
        match_mode = auto_outbound_echo_match_mode(value, reference)
        if not match_mode:
            continue
        ts = float(entry.get("ts", 0.0) or 0.0)
        return {
            "match_mode": match_mode,
            "source": str(entry.get("source") or "").strip(),
            "age_seconds": round(max(0.0, now - ts), 2) if ts else None,
            "reference_text": reference,
        }
    return {}
