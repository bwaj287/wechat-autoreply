from __future__ import annotations

from collections import Counter
import copy
import hashlib
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from .capture_cleanup import cleanup_runtime_artifacts_older_than
from .claim_policy import (
    candidate_contact_name,
    canonical_contact_match_key as _canonical_contact_match_key,
    choose_non_whitelist_unread,
    choose_whitelist_candidates,
    contact_badge_streak as _contact_badge_streak,
    decide_claim,
    evaluate_passive_preflight,
    filter_candidates_by_badge_streak as _filter_candidates_by_badge_streak,
    has_row_numeric_unread_badge as _has_row_numeric_unread_badge,
    match_allowed_contact as _match_allowed_contact,
    menu_signal_rising,
    update_badge_streaks as _update_badge_streaks,
)
from .contact_memory import get_contact_memory, remember_contact_memory
from .config_store import load_config
from .erge_client import ErgeClient
from .event_log import append_event
from .idle import get_idle_time_seconds
from .manual_reply_policy import detect_manual_reply_trigger
from .ollama_client import OllamaClient
from .outbound_history import (
    compact_message_text as _compact_message_text,
    draft_match_mode as _draft_match_mode,
    match_recent_auto_outbound as _match_recent_auto_outbound,
    message_similarity_score as _message_similarity_score,
    messages_overlap as _messages_overlap,
    prune_recent_auto_outbounds as _prune_recent_auto_outbounds,
    recent_auto_outbound_ttl as _recent_auto_outbound_ttl,
    remember_recent_auto_outbound as _remember_recent_auto_outbound,
)
from .paths import CAPTURE_DIR
from .pending_queue import (
    find_queue_index_for_contact,
    get_pending_queue,
    prune_stale_pending,
    queued_contacts,
    remove_pending_by_fingerprint,
    sync_pending_state,
)
from .recheck_policy import build_recheck_consensus
from .state_store import load_state, save_state, utc_now_iso
from .vision import check_unread_dot, unread_signal
from . import wechat_ui

HISTORY_MARKER_RE = re.compile(
    r"(?i)^(?:(?:today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|wed|thu|fri|sat|sun|今天|昨天|前天)\s+)?\d{1,2}:\d{2}$"
)
DIGIT_PUNCT_SHORT_RE = re.compile(r"^[0-9０-９]+[~～`'\"!！?？.,，。…·•\-_/\\|]*$")
SYMBOL_ONLY_SHORT_RE = re.compile(r"^[~～`'\"!！?？.,，。…·•\-_/\\|]+$")
SHORT_PING_RE = re.compile(r"^[?？!！]{1,3}$")
QUOTED_REPLY_CARD_RE = re.compile(r"^\s*([^:：\n]{1,40})\s*[:：]\s*(.+)$")
STALE_SYSTEM_INBOUND_RE = re.compile(
    r"(?i)\b("
    r"already answered elsewhere|"
    r"call cancel(?:ed|led) by caller|"
    r"voice call|"
    r"video call|"
    r"call not answered|"
    r"missed call|"
    r"recalled a message"
    r")\b"
)
CARD_METADATA_HINT_RE = re.compile(
    r"(?i)(?:小红书|笔记|shares?\b|share\b|note\b|公众号|article\b|链接|link\b|视频号|playlist\b)"
)
MEDIA_PREVIEW_PREFIX_RE = re.compile(
    r"(?i)^\s*[\[\(（【<〈《［]\s*(?:link|photo|picture|image|pic|sticker|emoji|video|图片|照片|相片|截图|表情包|贴纸|视频)"
    r"(?:[^\]\)）】>〉》］]{0,12})?[\]\)）】>〉》］]"
)
MAX_INTERNAL_UI_SUPPRESSION_SECONDS = 0.45


def normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def is_actionable_menu_signal(signal: str) -> bool:
    value = str(signal or "").strip()
    return value.isdigit() and int(value) > 0


def normalized_message_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in str(text or "").splitlines():
        value = normalize_text(raw)
        if not value or is_history_marker(value):
            continue
        lines.append(value)
    return lines


def fingerprint(contact: str, inbound_text: str, message_time: str = "") -> str:
    raw = f"{normalize_text(contact)}\0{normalize_text(message_time)}\0{normalize_text(inbound_text)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def is_history_marker(text: str) -> bool:
    return bool(HISTORY_MARKER_RE.fullmatch(" ".join((text or "").strip().split())))


def is_stale_system_inbound_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    compact = normalize_text(value)
    if not compact:
        return False
    return bool(STALE_SYSTEM_INBOUND_RE.search(compact))


def _meaningful_inbound_items(panel: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in list(panel.get("inbound") or []):
        text = str(item.get("text") or "").strip()
        if not wechat_ui.has_meaningful_text(text) or is_history_marker(text):
            continue
        if _is_likely_ocr_noise_line(text):
            continue
        enriched = copy.deepcopy(item) if isinstance(item, dict) else {}
        enriched["text"] = text
        enriched["top"] = float(item.get("top", 0.0) or 0.0)
        items.append(enriched)
    items.sort(key=lambda item: float(item.get("top", 0.0) or 0.0))
    return items


def _meaningful_outbound_items(panel: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in list(panel.get("outbound") or []):
        text = str(item.get("text") or "").strip()
        if not wechat_ui.has_meaningful_text(text) or is_history_marker(text):
            continue
        if _is_likely_ocr_noise_line(text):
            continue
        enriched = copy.deepcopy(item) if isinstance(item, dict) else {}
        enriched["text"] = text
        enriched["top"] = float(item.get("top", 0.0) or 0.0)
        items.append(enriched)
    items.sort(key=lambda item: float(item.get("top", 0.0) or 0.0))
    return items


def _tail_cluster(items: list[dict[str, Any]], max_gap: float = 0.14) -> list[dict[str, Any]]:
    if not items:
        return []
    tops = [round(float(item.get("top", 0.0) or 0.0), 3) for item in items]
    if len(set(tops)) <= 1:
        return [items[-1]]
    cluster = [items[-1]]
    last_top = float(items[-1].get("top", 0.0) or 0.0)
    for item in reversed(items[:-1]):
        top = float(item.get("top", 0.0) or 0.0)
        if last_top - top > max_gap:
            break
        cluster.append(item)
        last_top = top
    cluster.reverse()
    return cluster


def _clean_multiline_latest_inbound(latest_inbound: str, preview: str) -> str:
    value = str(latest_inbound or "").strip()
    if not value:
        return ""
    lines = [line.strip() for line in value.splitlines() if line.strip() and not is_history_marker(line)]
    if not lines:
        return ""
    lines = _sanitize_inbound_lines(lines, preview)
    if not lines:
        return ""
    preview_value = str(preview or "").strip()
    if preview_value:
        preview_norm = normalize_text(preview_value)
        if preview_norm == normalize_text(lines[-1]):
            return lines[-1]
    if len(lines) == 1:
        return lines[0]
    return lines[-1]


def _strip_quote_sender_prefix(text: str) -> tuple[str, str]:
    value = str(text or "").strip()
    if not value:
        return "", ""
    match = QUOTED_REPLY_CARD_RE.match(value)
    if not match:
        return "", value
    return str(match.group(1) or "").strip(), str(match.group(2) or "").strip()


def _find_outbound_quote_match(panel: dict[str, Any], text: str) -> dict[str, str]:
    value = str(text or "").strip()
    if not value:
        return {}
    quoted_sender, stripped = _strip_quote_sender_prefix(value)
    outbound_items = _meaningful_outbound_items(panel)
    if not outbound_items:
        return {}
    candidates = [value]
    if stripped and normalize_text(stripped) != normalize_text(value):
        candidates.insert(0, stripped)
    for candidate in candidates:
        candidate_norm = normalize_text(candidate)
        if not candidate_norm:
            continue
        for outbound in reversed(outbound_items):
            outbound_text = str(outbound.get("text") or "").strip()
            if not outbound_text:
                continue
            if (
                candidate_norm == normalize_text(outbound_text)
                or _messages_overlap(candidate, outbound_text)
                or _message_similarity_score(candidate, outbound_text) >= 0.84
            ):
                return {
                    "text": outbound_text,
                    "role": "self",
                    "sender": quoted_sender,
                }
    return {}


def _extract_quote_payload(
    panel: dict[str, Any],
    rows: list[dict[str, Any]],
    preview: str = "",
) -> dict[str, str]:
    if len(rows) < 2:
        return {}
    quote_index = None
    quote_match: dict[str, str] = {}
    for idx, item in enumerate(rows[1:], start=1):
        quote_match = _find_outbound_quote_match(panel, str(item.get("text") or "").strip())
        if quote_match:
            quote_index = idx
            break
    if quote_index is None or not quote_match:
        return {}
    main_lines = _sanitize_inbound_lines(
        [str(item.get("text", "")).strip() for item in rows[:quote_index] if item.get("text")],
        preview,
    )
    main_text = "\n".join(main_lines) if main_lines else ""
    if not main_text:
        return {}
    return {
        "text": main_text,
        "quoted_text": str(quote_match.get("text") or "").strip(),
        "quoted_role": str(quote_match.get("role") or "").strip() or "unknown",
        "quoted_sender": str(quote_match.get("sender") or "").strip(),
    }


def extract_inbound_payload(panel: dict[str, Any], fallback_preview: str = "") -> dict[str, str]:
    preview = str(fallback_preview or "").strip()
    latest_outbound = str(panel.get("latestOutbound") or "").strip()
    preview_is_outbound = _text_matches_outbound(preview, latest_outbound)
    payload = {
        "text": "",
        "quoted_text": "",
        "quoted_role": "",
        "quoted_sender": "",
    }
    inbound_items = _meaningful_inbound_items(panel)
    raw_inbound_items = [
        copy.deepcopy(item)
        for item in list(panel.get("inbound") or [])
        if str(item.get("text") or "").strip() and not is_history_marker(str(item.get("text") or "").strip())
    ]
    if inbound_items:
        latest_outbound_top = max(
            (
                float(item.get("top", 0.0) or 0.0)
                for item in list(panel.get("outbound") or [])
                if str(item.get("text") or "").strip()
            ),
            default=None,
        )
        if latest_outbound_top is not None:
            recent_rows = _tail_cluster(
                [
                    item
                    for item in inbound_items
                    if float(item.get("top", 0.0) or 0.0) > latest_outbound_top + 0.01
                ]
            )
            if recent_rows:
                quote_payload = _extract_quote_payload(panel, recent_rows, preview)
                if quote_payload.get("text"):
                    return quote_payload
                lines = [
                    str(item.get("text", "")).strip()
                    for item in recent_rows
                    if item.get("text")
                ]
                lines = _sanitize_inbound_lines(lines, preview)
                if lines:
                    payload["text"] = "\n".join(lines)
                    return payload
                short_recovery = _recover_short_inbound_text(
                    *[str(item.get("text", "")).strip() for item in recent_rows],
                    preview,
                )
                if short_recovery:
                    payload["text"] = short_recovery
                    return payload
            raw_recent_rows = _tail_cluster(
                [
                    item
                    for item in raw_inbound_items
                    if float(item.get("top", 0.0) or 0.0) > latest_outbound_top + 0.01
                ]
            )
            if raw_recent_rows:
                short_recovery = _recover_short_inbound_text(
                    *[str(item.get("text", "")).strip() for item in raw_recent_rows],
                    preview,
                )
                if short_recovery:
                    payload["text"] = short_recovery
                    return payload
        tail_rows = _tail_cluster(inbound_items)
        if tail_rows:
            quote_payload = _extract_quote_payload(panel, tail_rows[-3:], preview)
            if quote_payload.get("text"):
                return quote_payload
            lines = [
                str(item.get("text", "")).strip()
                for item in tail_rows[-3:]
                if item.get("text")
            ]
            lines = _sanitize_inbound_lines(lines, preview)
            if lines:
                payload["text"] = "\n".join(lines)
                return payload
            short_recovery = _recover_short_inbound_text(
                *[str(item.get("text", "")).strip() for item in tail_rows[-3:]],
                preview,
            )
            if short_recovery:
                payload["text"] = short_recovery
                return payload
    raw_tail_rows = _tail_cluster(raw_inbound_items)
    if raw_tail_rows:
        short_recovery = _recover_short_inbound_text(
            *[str(item.get("text", "")).strip() for item in raw_tail_rows[-3:]],
            preview,
        )
        if short_recovery:
            payload["text"] = short_recovery
            return payload
    latest_inbound = str(panel.get("latestInbound") or "").strip()
    if is_history_marker(latest_inbound):
        latest_inbound = ""
    cleaned_latest_inbound = _clean_multiline_latest_inbound(latest_inbound, preview)
    if cleaned_latest_inbound:
        latest_inbound = cleaned_latest_inbound
    elif _is_likely_ocr_noise_line(latest_inbound):
        latest_inbound = ""
    if is_history_marker(preview):
        preview = ""
        preview_is_outbound = False
    if preview_is_outbound:
        preview = ""
    if wechat_ui.is_nontext_message(preview):
        payload["text"] = preview
        return payload
    short_recovery = _recover_short_inbound_text(latest_inbound, preview)
    if short_recovery:
        payload["text"] = short_recovery
        return payload
    if wechat_ui.has_meaningful_text(latest_inbound):
        payload["text"] = latest_inbound
        return payload
    if wechat_ui.has_meaningful_text(preview):
        payload["text"] = preview
        return payload
    payload["text"] = latest_inbound or preview
    return payload


def _text_matches_outbound(preview: str, latest_outbound: str) -> bool:
    preview_norm = normalize_text(preview)
    outbound_norm = normalize_text(latest_outbound)
    if not preview_norm or not outbound_norm:
        return False
    return (
        preview_norm == outbound_norm
        or preview_norm in outbound_norm
        or outbound_norm in preview_norm
    )


def _is_likely_ocr_noise_line(line: str) -> bool:
    value = str(line or "").strip()
    if not value:
        return True
    if is_history_marker(value):
        return True
    if SHORT_PING_RE.fullmatch(value):
        return False
    if wechat_ui.is_nontext_message(value):
        return False
    if re.fullmatch(r"[A-Za-z]", value):
        return False
    if re.search(r"[\u4e00-\u9fffA-Za-z]", value):
        return False
    normalized = normalize_text(value)
    if len(normalized) <= 4 and DIGIT_PUNCT_SHORT_RE.fullmatch(value):
        return True
    if len(normalized) <= 4 and SYMBOL_ONLY_SHORT_RE.fullmatch(value):
        return True
    if len(normalized) <= 2 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", value):
        return True
    return False


def _sanitize_inbound_lines(lines: list[str], preview: str = "") -> list[str]:
    cleaned = [str(line).strip() for line in lines if str(line).strip()]
    if not cleaned:
        return []

    deduped: list[str] = []
    for line in cleaned:
        if deduped and normalize_text(deduped[-1]) == normalize_text(line):
            continue
        deduped.append(line)
    cleaned = deduped

    preview_value = str(preview or "").strip()
    preview_norm = normalize_text(preview_value)
    preview_is_meaningful = wechat_ui.has_meaningful_text(preview_value) and not is_history_marker(preview_value)
    has_preview_line = bool(preview_norm) and any(normalize_text(line) == preview_norm for line in cleaned)

    # Trim noisy tail fragments such as OCR artifacts ("8~", isolated punctuation).
    while len(cleaned) > 1:
        tail = cleaned[-1]
        if not _is_likely_ocr_noise_line(tail):
            break
        if has_preview_line and normalize_text(tail) != preview_norm:
            cleaned.pop()
            continue
        # If preview is meaningful and tail is noisy-but-different, trust preview.
        if preview_is_meaningful and preview_norm and normalize_text(tail) != preview_norm:
            cleaned[-1] = preview_value
            break
        break

    if (
        len(cleaned) == 1
        and preview_is_meaningful
        and preview_norm
        and normalize_text(cleaned[0]) != preview_norm
        and _is_likely_ocr_noise_line(cleaned[0])
    ):
        cleaned = [preview_value]

    if len(cleaned) == 1 and _is_likely_ocr_noise_line(cleaned[0]):
        return []

    return [line for line in cleaned if line]


def _recover_short_inbound_text(*values: str) -> str:
    for raw in values:
        value = str(raw or "").strip()
        if not value or is_history_marker(value):
            continue
        if SHORT_PING_RE.fullmatch(value):
            return value
        if re.fullmatch(r"[A-Za-z]", value):
            return value
        compact = _compact_message_text(value)
        if re.fullmatch(r"[A-Za-z]", compact):
            return compact
        if compact and len(compact) <= 2:
            if re.search(r"[\u4e00-\u9fffA-Za-z]", compact):
                return value
            return "[收到一条超短消息]"
    return ""


def _looks_like_card_metadata_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if CARD_METADATA_HINT_RE.search(normalize_text(value)):
        return True
    lines = _sanitize_inbound_lines(value.splitlines())
    if len(lines) >= 2 and CARD_METADATA_HINT_RE.search(normalize_text(" ".join(lines))):
        return True
    return False


def _looks_like_media_preview_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(MEDIA_PREVIEW_PREFIX_RE.search(value))


def _card_or_media_variant_equivalent(first: str, second: str) -> bool:
    first_value = str(first or "").strip()
    second_value = str(second or "").strip()
    if not first_value or not second_value:
        return False

    first_media = wechat_ui.is_media_message(first_value) or _looks_like_media_preview_text(first_value)
    second_media = wechat_ui.is_media_message(second_value) or _looks_like_media_preview_text(second_value)
    first_card = _looks_like_card_metadata_text(first_value)
    second_card = _looks_like_card_metadata_text(second_value)

    if not (first_media or second_media or first_card or second_card):
        return False

    first_lines = _sanitize_inbound_lines(first_value.splitlines())
    second_lines = _sanitize_inbound_lines(second_value.splitlines())
    if not first_lines or not second_lines:
        return False

    if (first_media and second_card) or (second_media and first_card):
        return True

    for left in first_lines:
        for right in second_lines:
            if _messages_overlap(left, right) or _message_similarity_score(left, right) >= 0.72:
                return True

    return False


def inbound_variant_equivalent(
    pending_text: str,
    current_text: str,
    *,
    pending_time: str = "",
    current_time: str = "",
) -> bool:
    if not normalize_text(pending_text) or not normalize_text(current_text):
        return False
    if normalize_text(pending_text) == normalize_text(current_text):
        return True
    if pending_time and current_time and normalize_text(pending_time) != normalize_text(current_time):
        return False
    pending_lines = normalized_message_lines(pending_text)
    current_lines = normalized_message_lines(current_text)
    if not pending_lines or not current_lines:
        return False
    shorter, longer = (
        (pending_lines, current_lines) if len(pending_lines) <= len(current_lines) else (current_lines, pending_lines)
    )
    if len(shorter) < len(longer) and longer[-len(shorter) :] == shorter:
        return True

    pending_norm = normalize_text(pending_text)
    current_norm = normalize_text(current_text)
    shorter_text, longer_text = (
        (pending_norm, current_norm) if len(pending_norm) <= len(current_norm) else (current_norm, pending_norm)
    )
    if _card_or_media_variant_equivalent(pending_text, current_text):
        return True
    if _fragment_like_same_message(shorter_text, longer_text):
        return True
    return False


def _fragment_like_same_message(shorter_text: str, longer_text: str) -> bool:
    shorter = normalize_text(shorter_text)
    longer = normalize_text(longer_text)
    if not shorter or not longer or shorter == longer:
        return False
    if len(shorter) > len(longer):
        shorter, longer = longer, shorter
    if len(shorter) > 10:
        return False
    if "…" not in longer and "..." not in longer:
        return False
    shorter_compact = _compact_message_text(shorter)
    longer_compact = _compact_message_text(longer)
    if not shorter_compact or not longer_compact:
        return False
    if len(shorter_compact) > 8 or len(longer_compact) <= len(shorter_compact):
        return False
    max_overlap = min(3, len(shorter_compact), len(longer_compact))
    overlap = 0
    for size in range(max_overlap, 0, -1):
        if longer_compact.endswith(shorter_compact[:size]):
            overlap = size
            break
    if overlap == 0:
        return False
    return len(shorter_compact) - overlap >= 2


def truncation_like_change(previous_text: str, current_text: str) -> bool:
    previous = normalize_text(previous_text)
    current = normalize_text(current_text)
    if not previous or not current or previous == current:
        return False
    if _fragment_like_same_message(previous, current):
        return True
    shorter, longer = (previous, current) if len(previous) <= len(current) else (current, previous)
    trimmed = shorter.rstrip("…。.,!?！？~～")
    if not trimmed:
        return False
    return longer.startswith(trimmed)


def preview_panel_equivalent(preview_text: str, panel_text: str) -> bool:
    preview = normalize_text(preview_text)
    panel = normalize_text(panel_text)
    if not preview or not panel:
        return False
    if preview == panel or preview in panel or panel in preview:
        return True
    preview_compact = re.sub(r"[\s…。,，!！?？~～·•`'\"\\-_/\\\\|]+", "", preview)
    panel_compact = re.sub(r"[\s…。,，!！?？~～·•`'\"\\-_/\\\\|]+", "", panel)
    if preview_compact and panel_compact and (
        preview_compact in panel_compact or panel_compact in preview_compact
    ):
        return True
    if truncation_like_change(preview_text, panel_text) or truncation_like_change(panel_text, preview_text):
        return True
    return False


def preview_reliable_for_fallback(preview_text: str) -> bool:
    value = normalize_text(preview_text)
    if not value:
        return False
    compact = re.sub(r"[\s…。,，!！?？~～·•`'\"\\-_/\\\\|]+", "", value)
    # Too-short previews are often ambiguous after truncation (e.g. "好的…", "在吗…").
    return len(compact) >= 5


def _line_matches_outbound_contamination(line: str, references: list[str]) -> bool:
    value = str(line or "").strip()
    if not value:
        return False
    compact = _compact_message_text(value)
    if len(compact) < 4:
        return False
    for reference in references:
        reference_value = str(reference or "").strip()
        if not reference_value:
            continue
        if _messages_overlap(value, reference_value) or _message_similarity_score(value, reference_value) >= 0.84:
            return True
        for reference_line in _sanitize_inbound_lines(reference_value.splitlines()):
            if _messages_overlap(value, reference_line) or _message_similarity_score(value, reference_line) >= 0.84:
                return True
    return False


def strip_outbound_contamination(
    pending_text: str,
    current_text: str,
    *references: str,
) -> tuple[str, list[str]]:
    pending_value = str(pending_text or "").strip()
    current_value = str(current_text or "").strip()
    if not pending_value or not current_value:
        return current_value, []

    pending_lines = _sanitize_inbound_lines(pending_value.splitlines())
    current_lines = _sanitize_inbound_lines(current_value.splitlines())
    if not pending_lines or not current_lines or len(current_lines) <= len(pending_lines):
        return current_value, []

    prefix_len = 0
    for pending_line, current_line in zip(pending_lines, current_lines):
        if normalize_text(pending_line) != normalize_text(current_line):
            break
        prefix_len += 1

    if prefix_len < len(pending_lines):
        return current_value, []

    extra_lines = [line for line in current_lines[prefix_len:] if str(line).strip()]
    if not extra_lines:
        return current_value, []

    contamination_refs = [str(reference or "").strip() for reference in references if str(reference or "").strip()]
    if not contamination_refs:
        return current_value, []

    if all(_line_matches_outbound_contamination(line, contamination_refs) for line in extra_lines):
        return pending_value or "\n".join(current_lines[:prefix_len]), extra_lines

    return current_value, []


def _is_ultra_short_fragment(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    if SHORT_PING_RE.fullmatch(value):
        return True
    compact = _compact_message_text(value)
    if not compact:
        return True
    if len(compact) <= 1:
        return True
    if len(compact) <= 4 and DIGIT_PUNCT_SHORT_RE.fullmatch(value):
        return True
    if len(compact) <= 4 and SYMBOL_ONLY_SHORT_RE.fullmatch(value):
        return True
    return False


def _outbound_item_right_aligned(item: dict[str, Any] | None) -> bool:
    if not isinstance(item, dict):
        return False
    left = float(item.get("left", 0.0) or 0.0)
    width = float(item.get("width", 0.0) or 0.0)
    right = left + width
    message_kind = str(item.get("messageKind", "text") or "text").strip().lower()
    green_pixels = int(item.get("greenPixels", 0) or 0)
    gray_pixels = int(item.get("grayPixels", 0) or 0)
    if message_kind == "media":
        return bool(left >= 0.58 or (left + width / 2.0) >= 0.68 or right >= 0.88)
    # Bubble color is the most direct signal in WeChat: green means self/outbound.
    if green_pixels >= 32 and green_pixels >= gray_pixels:
        return True
    return bool(
        (left >= 0.60 and right >= 0.84)
        or left >= 0.66
        or right >= 0.90
    )


def _inbound_item_gray_confirmed(item: dict[str, Any] | None) -> bool:
    if not isinstance(item, dict):
        return False
    left = float(item.get("left", 0.0) or 0.0)
    width = float(item.get("width", 0.0) or 0.0)
    right = left + width
    center = left + width / 2.0
    message_kind = str(item.get("messageKind", "text") or "text").strip().lower()
    green_pixels = int(item.get("greenPixels", 0) or 0)
    gray_pixels = int(item.get("grayPixels", 0) or 0)
    if message_kind == "media":
        return bool(left <= 0.50 and center <= 0.56 and right <= 0.86)
    if gray_pixels >= 24 and gray_pixels >= max(green_pixels + 6, int(green_pixels * 1.15)):
        return True
    return bool(
        gray_pixels >= 12
        and gray_pixels >= green_pixels
        and left <= 0.62
        and right <= 0.88
    )


def _manual_reply_signal_is_reliable(
    *,
    current_outbound: str,
    current_inbound: str,
    current_outbound_item: dict[str, Any] | None,
    current_outbound_top: float | None,
    latest_inbound_top: float | None,
    latest_bubble_outbound: bool,
    has_chat_window: bool,
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if not current_outbound:
        blockers.append("no_outbound_text")
    if current_outbound_item is None:
        blockers.append("missing_outbound_item")
    elif not _outbound_item_right_aligned(current_outbound_item):
        blockers.append("outbound_not_right_aligned")
    if current_inbound and _messages_overlap(current_outbound, current_inbound):
        blockers.append("outbound_overlaps_inbound")
    if current_inbound and _is_ultra_short_fragment(current_outbound):
        blockers.append("outbound_too_short_with_inbound")
    if (
        current_outbound_top is not None
        and latest_inbound_top is not None
        and current_outbound_top <= latest_inbound_top + 0.02
    ):
        blockers.append("outbound_not_after_inbound")
    if not has_chat_window and not latest_bubble_outbound:
        blockers.append("roster_without_latest_outbound")
    return (len(blockers) == 0, blockers)


def choose_inbound_text(panel: dict[str, Any], fallback_preview: str = "") -> str:
    return str(extract_inbound_payload(panel, fallback_preview).get("text") or "").strip()


def _is_context_text_candidate(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if is_history_marker(value):
        return False
    if not wechat_ui.has_meaningful_text(value):
        return False
    if _is_likely_ocr_noise_line(value):
        return False
    return True


def _matches_quoted_message(text: str, quoted_message: dict[str, str] | None) -> bool:
    if not isinstance(quoted_message, dict):
        return False
    value = str(text or "").strip()
    quoted_text = str(quoted_message.get("quoted_text") or "").strip()
    if not value or not quoted_text:
        return False
    _, stripped = _strip_quote_sender_prefix(value)
    candidates = [value]
    if stripped and normalize_text(stripped) != normalize_text(value):
        candidates.insert(0, stripped)
    for candidate in candidates:
        if (
            normalize_text(candidate) == normalize_text(quoted_text)
            or _messages_overlap(candidate, quoted_text)
            or _message_similarity_score(candidate, quoted_text) >= 0.88
        ):
            return True
    return False


def build_reply_context(
    panel: dict[str, Any],
    inbound_text: str,
    *,
    max_messages: int = 8,
    quoted_message: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    limit = max(0, int(max_messages))
    if limit <= 0:
        return []

    payload = panel if isinstance(panel, dict) else {}
    timeline: list[dict[str, Any]] = []
    for item in list(payload.get("inbound") or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not _is_context_text_candidate(text):
            continue
        if _matches_quoted_message(text, quoted_message):
            continue
        timeline.append(
            {
                "role": "contact",
                "text": text,
                "top": float(item.get("top", 0.0) or 0.0),
            }
        )
    for item in list(payload.get("outbound") or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not _is_context_text_candidate(text):
            continue
        timeline.append(
            {
                "role": "self",
                "text": text,
                "top": float(item.get("top", 0.0) or 0.0),
            }
        )
    timeline.sort(key=lambda entry: (float(entry.get("top", 0.0) or 0.0), 1 if entry.get("role") == "self" else 0))

    context: list[dict[str, str]] = []
    for entry in timeline:
        role = "self" if str(entry.get("role") or "").strip().lower() == "self" else "contact"
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        if context and context[-1]["role"] == role and normalize_text(context[-1]["text"]) == normalize_text(text):
            continue
        context.append({"role": role, "text": text})

    latest_inbound = str(inbound_text or "").strip()
    if _is_context_text_candidate(latest_inbound):
        merged = False
        for idx in range(len(context) - 1, -1, -1):
            item = context[idx]
            if item.get("role") != "contact":
                continue
            current_text = str(item.get("text") or "")
            if (
                preview_panel_equivalent(current_text, latest_inbound)
                or _messages_overlap(current_text, latest_inbound)
                or _message_similarity_score(current_text, latest_inbound) >= 0.88
            ):
                context[idx]["text"] = latest_inbound
                merged = True
                break
        if not merged:
            context.append({"role": "contact", "text": latest_inbound})

    if len(context) > limit:
        context = context[-limit:]
    return context


def _safe_contact_token(contact: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", str(contact or "").strip())
    token = token.strip("_")
    return token or "unknown"


def _snapshot_debug_capture(src_path: str, *, contact: str, now: float, label: str) -> str:
    value = str(src_path or "").strip()
    if not value:
        return ""
    source = Path(value)
    if not source.exists() or not source.is_file():
        return ""
    try:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return ""


def _preferred_chat_screenshot(selected: dict[str, Any] | None) -> str:
    payload = selected if isinstance(selected, dict) else {}
    screenshots = payload.get("screenshots") or {}
    chat_path = str(screenshots.get("chat") or "").strip()
    if chat_path:
        return chat_path
    return str(payload.get("screenshot") or "").strip()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    ms = int((now - int(now)) * 1000)
    ext = source.suffix or ".png"
    dest = CAPTURE_DIR / f"debug-empty-inbound-{stamp}-{ms:03d}-{_safe_contact_token(contact)}-{label}{ext}"
    try:
        shutil.copy2(source, dest)
        return str(dest)
    except Exception:
        return ""


def _panel_debug_summary(panel: dict[str, Any]) -> dict[str, Any]:
    inbound = list(panel.get("inbound") or [])
    outbound = list(panel.get("outbound") or [])
    misc = list(panel.get("misc") or [])
    return {
        "title": str(panel.get("title") or ""),
        "latestInbound": str(panel.get("latestInbound") or ""),
        "latestOutbound": str(panel.get("latestOutbound") or ""),
        "inboundCount": len(inbound),
        "outboundCount": len(outbound),
        "miscCount": len(misc),
        "inboundTail": [str(item.get("text") or "") for item in inbound[-3:]],
        "outboundTail": [str(item.get("text") or "") for item in outbound[-3:]],
    }


def _empty_inbound_debug_payload(
    *,
    contact: str,
    now: float,
    selected: dict[str, Any],
    panel: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    screenshots = selected.get("screenshots") or {}
    roster_src = str(screenshots.get("roster") or "")
    chat_src = str(screenshots.get("chat") or "")
    active_src = str(selected.get("screenshot") or "")
    return {
        "contact": contact,
        "active_chat": str(selected.get("activeChat") or ""),
        "selection_confirmed": bool(selected.get("selectionConfirmed")),
        "selected_requested": str(selected.get("selectedChatRequested") or ""),
        "candidate": {
            "name": str(candidate.get("name") or ""),
            "matchedContact": str(candidate.get("matchedContact") or ""),
            "preview": str(candidate.get("preview") or ""),
            "time": str(candidate.get("time") or ""),
            "unread": bool(candidate.get("unread")),
            "redPixelCount": int(candidate.get("redPixelCount", 0) or 0),
        },
        "panel_summary": _panel_debug_summary(panel),
        "captures": {
            "probe_screenshot": active_src,
            "probe_roster": roster_src,
            "probe_chat": chat_src,
            "saved_probe_screenshot": _snapshot_debug_capture(active_src, contact=contact, now=now, label="active"),
            "saved_probe_roster": _snapshot_debug_capture(roster_src, contact=contact, now=now, label="roster"),
            "saved_probe_chat": _snapshot_debug_capture(chat_src, contact=contact, now=now, label="chat"),
        },
    }


def choose_active_whitelist_candidate(probe_result: dict[str, Any], allowed_contacts: list[str]) -> dict[str, Any]:
    active_chat = str(probe_result.get("activeChat") or "").strip()
    if not active_chat:
        return {}
    matched_contact = _match_allowed_contact(active_chat, allowed_contacts)
    if not matched_contact:
        return {}
    visible = find_visible_chat(probe_result, matched_contact)
    if not visible:
        return {}
    visible_chats = list(probe_result.get("visibleChats", []) or [])
    if visible_chats:
        top_min = min(float(chat.get("ocrTop", 1.0) or 1.0) for chat in visible_chats)
        active_top = float(visible.get("ocrTop", 1.0) or 1.0)
        # Only rescue the active chat when it is effectively the top row that just
        # bubbled up. This keeps the fix scoped to "WeChat auto-selected the unread
        # chat and cleared its badge on open" instead of reviving broad fallbacks.
        if active_top > top_min + 0.08:
            return {}
    panel = probe_result.get("chatPanel", {}) or {}
    if not latest_panel_bubble_is_inbound_gray(panel):
        return {}
    inbound_text = choose_inbound_text(panel, str(visible.get("preview") or ""))
    if not inbound_text:
        return {}
    latest_outbound = latest_committed_outbound(panel)
    if _text_matches_outbound(inbound_text, latest_outbound):
        return {}
    return {
        "name": active_chat,
        "matchedContact": matched_contact,
        "preview": str(visible.get("preview") or ""),
        "time": str(visible.get("time") or ""),
        "source": "active_chat_fallback",
    }


def panel_has_claim_signal(panel: dict[str, Any]) -> bool:
    payload = panel if isinstance(panel, dict) else {}
    return bool(
        payload.get("latestInbound")
        or payload.get("latestOutbound")
        or payload.get("inbound")
        or payload.get("outbound")
        or payload.get("misc")
    )


def choose_whitelist_preview_fallback_candidate(
    probe_result: dict[str, Any],
    allowed_contacts: list[str],
    *,
    queue: list[dict[str, Any]],
    last_seen_inbound: dict[str, str],
) -> dict[str, Any]:
    active_chat = str(probe_result.get("activeChat") or "").strip()
    active_panel = probe_result.get("chatPanel", {}) or {}
    active_latest_outbound = latest_message_is_outbound(active_panel)
    visible_chats = sorted(
        list(probe_result.get("visibleChats", []) or []),
        key=lambda chat: (
            -1 if _has_row_numeric_unread_badge(chat) else 0,
            -int(chat.get("digitPixelCount", 0) or 0),
            -int(chat.get("redPixelCount", 0) or 0),
            float(chat.get("ocrTop", 0.0) or 0.0),
        ),
    )
    for chat in visible_chats:
        # Preview fallback is conservative: only rows with explicit numeric unread
        # badge evidence are allowed into claim flow.
        if not _has_row_numeric_unread_badge(chat):
            continue
        matched_contact = _match_allowed_contact(str(chat.get("name", "")), allowed_contacts)
        if not matched_contact:
            continue
        if (
            active_chat
            and active_latest_outbound
            and _canonical_contact_match_key(matched_contact) == _canonical_contact_match_key(active_chat)
        ):
            continue
        preview = str(chat.get("preview") or "").strip()
        if not preview or is_history_marker(preview) or not wechat_ui.has_meaningful_text(preview):
            continue
        if not preview_reliable_for_fallback(preview):
            continue
        message_time = str(chat.get("time") or "")
        preview_fp = fingerprint(matched_contact, preview, message_time)
        if str(last_seen_inbound.get(matched_contact, "")) == preview_fp:
            continue
        existing_index = find_queue_index_for_contact(queue, matched_contact)
        if existing_index >= 0:
            existing_fp = str(queue[existing_index].get("inbound_fingerprint", ""))
            if existing_fp == preview_fp:
                continue
        candidate = copy.deepcopy(chat)
        candidate["matchedContact"] = matched_contact
        candidate["source"] = "preview_fallback"
        return candidate
    return {}


def _preview_fingerprint_already_recorded(
    *,
    contact: str,
    preview: str,
    message_time: str,
    queue: list[dict[str, Any]],
    last_seen_inbound: dict[str, str],
) -> bool:
    preview_fp = fingerprint(contact, preview, message_time)
    if str(last_seen_inbound.get(contact, "")) == preview_fp:
        return True
    existing_index = find_queue_index_for_contact(queue, contact)
    if existing_index >= 0:
        return str(queue[existing_index].get("inbound_fingerprint", "")) == preview_fp
    return False


def choose_global_signal_whitelist_probe_candidates(
    probe_result: dict[str, Any],
    allowed_contacts: list[str],
    *,
    queue: list[dict[str, Any]],
    last_seen_inbound: dict[str, str],
    max_search_contacts: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_candidate_keys: set[str] = set()
    visible_contact_keys: set[str] = set()
    active_chat = str(probe_result.get("activeChat") or "").strip()
    active_panel = probe_result.get("chatPanel", {}) or {}
    active_latest_outbound = latest_message_is_outbound(active_panel)
    visible_chats = sorted(
        list(probe_result.get("visibleChats", []) or []),
        key=lambda chat: float(chat.get("ocrTop", 0.0) or 0.0),
    )
    for chat in visible_chats:
        matched_contact = _match_allowed_contact(str(chat.get("name", "")), allowed_contacts)
        if not matched_contact:
            continue
        contact_key = _canonical_contact_match_key(matched_contact)
        if contact_key:
            visible_contact_keys.add(contact_key)
        if _has_row_numeric_unread_badge(chat):
            continue
        if (
            active_chat
            and active_latest_outbound
            and _canonical_contact_match_key(matched_contact) == _canonical_contact_match_key(active_chat)
        ):
            continue
        preview = str(chat.get("preview") or "").strip()
        message_time = str(chat.get("time") or "")
        if not preview or is_history_marker(preview) or not wechat_ui.has_meaningful_text(preview):
            continue
        if not preview_reliable_for_fallback(preview):
            continue
        if _preview_fingerprint_already_recorded(
            contact=matched_contact,
            preview=preview,
            message_time=message_time,
            queue=queue,
            last_seen_inbound=last_seen_inbound,
        ):
            continue
        candidate = copy.deepcopy(chat)
        candidate["matchedContact"] = matched_contact
        candidate["source"] = "global_signal_visible_whitelist_probe"
        candidates.append(candidate)
        if contact_key:
            seen_candidate_keys.add(contact_key)

    search_limit = max(0, int(max_search_contacts or 0))
    if search_limit <= 0:
        return candidates
    search_slots = max(0, search_limit - len(candidates))
    if search_slots <= 0:
        return candidates

    queue_order = {
        _canonical_contact_match_key(contact): index
        for index, contact in enumerate(queued_contacts(queue))
        if _canonical_contact_match_key(contact)
    }
    seen_order = {
        _canonical_contact_match_key(contact): index
        for index, contact in enumerate(last_seen_inbound.keys())
        if _canonical_contact_match_key(contact)
    }
    eligible_search_keys = set(queue_order) | set(seen_order)
    if not eligible_search_keys:
        return candidates
    indexed_contacts = list(enumerate(allowed_contacts))
    indexed_contacts.sort(
        key=lambda item: (
            0 if _canonical_contact_match_key(item[1]) in queue_order else 1,
            queue_order.get(_canonical_contact_match_key(item[1]), 9999),
            0 if _canonical_contact_match_key(item[1]) in seen_order else 1,
            seen_order.get(_canonical_contact_match_key(item[1]), 9999),
            item[0],
        )
    )
    added_search = 0
    for _, contact in indexed_contacts:
        contact_name = str(contact or "").strip()
        contact_key = _canonical_contact_match_key(contact_name)
        if not contact_name or not contact_key:
            continue
        if contact_key not in eligible_search_keys:
            continue
        if contact_key in seen_candidate_keys or contact_key in visible_contact_keys:
            continue
        candidates.append(
            {
                "name": contact_name,
                "matchedContact": contact_name,
                "preview": "",
                "time": "",
                "source": "global_signal_whitelist_search",
                "unread": False,
            }
        )
        seen_candidate_keys.add(contact_key)
        added_search += 1
        if added_search >= search_slots:
            break
    return candidates


def find_visible_chat(probe_result: dict[str, Any], contact: str) -> dict[str, Any]:
    for chat in probe_result.get("visibleChats", []):
        if wechat_ui.names_match(str(chat.get("name", "")), contact):
            return chat
    return {}


def latest_committed_outbound(panel: dict[str, Any], max_top: float = 0.90) -> str:
    item = latest_committed_outbound_item(panel, max_top=max_top)
    if not item:
        if list(panel.get("outbound") or []):
            return ""
        return str(panel.get("latestOutbound") or "").strip()
    return str(item.get("text", "")).strip()


def latest_committed_outbound_item(panel: dict[str, Any], max_top: float = 0.90) -> dict[str, Any]:
    outbound = list(panel.get("outbound") or [])
    if not outbound:
        return {}
    committed = [item for item in outbound if float(item.get("top", 1.0) or 1.0) <= max_top]
    if not committed:
        return {}
    return committed[-1]


def latest_meaningful_inbound_top(panel: dict[str, Any]) -> float | None:
    inbound_items = _meaningful_inbound_items(panel)
    if not inbound_items:
        return None
    return max(float(item.get("top", 0.0) or 0.0) for item in inbound_items)


def latest_meaningful_inbound_item(panel: dict[str, Any]) -> dict[str, Any]:
    inbound_items = _meaningful_inbound_items(panel)
    if not inbound_items:
        return {}
    return inbound_items[-1]


def latest_message_is_outbound(panel: dict[str, Any], *, max_top: float = 0.90, epsilon: float = 0.01) -> bool:
    outbound_item = latest_committed_outbound_item(panel, max_top=max_top)
    if not outbound_item:
        return False
    outbound_text = str(outbound_item.get("text") or "").strip()
    if not wechat_ui.has_meaningful_text(outbound_text) or is_history_marker(outbound_text):
        return False
    inbound_top = latest_meaningful_inbound_top(panel)
    if inbound_top is None:
        # OCR can intermittently miss the latest inbound bubble; when we still
        # have a right-aligned outbound bubble, treat it as "my latest message".
        return _outbound_item_right_aligned(outbound_item)
    outbound_top = float(outbound_item.get("top", 0.0) or 0.0)
    return outbound_top > inbound_top + epsilon


def latest_panel_bubble_is_inbound_gray(
    panel: dict[str, Any],
    *,
    max_top: float = 0.90,
    epsilon: float = 0.01,
) -> bool:
    inbound_item = latest_meaningful_inbound_item(panel)
    if not inbound_item or not _inbound_item_gray_confirmed(inbound_item):
        return False
    inbound_top = float(inbound_item.get("top", 0.0) or 0.0)
    outbound_item = latest_committed_outbound_item(panel, max_top=max_top)
    if not outbound_item:
        return True
    outbound_text = str(outbound_item.get("text") or "").strip()
    if not wechat_ui.has_meaningful_text(outbound_text) or is_history_marker(outbound_text):
        return True
    outbound_top = float(outbound_item.get("top", 0.0) or 0.0)
    return inbound_top > outbound_top + epsilon


def panel_tail_slice(panel: dict[str, Any], *, min_top: float = 0.52, span: float = 0.28) -> dict[str, Any]:
    inbound_all = list(panel.get("inbound") or [])
    outbound_all = list(panel.get("outbound") or [])
    misc_all = list(panel.get("misc") or [])
    all_items = [item for item in inbound_all + outbound_all if str(item.get("text") or "").strip()]
    if not all_items:
        return dict(panel)
    tops = [float(item.get("top", 0.0) or 0.0) for item in all_items]
    anchor_top = max(tops)
    cutoff = max(float(min_top), anchor_top - float(span))

    def _keep(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = [item for item in items if float(item.get("top", 0.0) or 0.0) >= cutoff]
        return kept if kept else items[-2:]

    inbound = _keep(inbound_all) if inbound_all else []
    outbound = _keep(outbound_all) if outbound_all else []
    misc = _keep(misc_all) if misc_all else []
    return {
        "title": panel.get("title", ""),
        "latestInbound": inbound[-1].get("text", "") if inbound else "",
        "latestOutbound": outbound[-1].get("text", "") if outbound else "",
        "inbound": inbound,
        "outbound": outbound,
        "misc": misc,
    }


def panel_tail_confidence(panel: dict[str, Any], *, max_items_per_side: int = 2) -> float | None:
    values: list[float] = []
    for side in ("inbound", "outbound"):
        items = list(panel.get(side) or [])
        if not items:
            continue
        for item in items[-max_items_per_side:]:
            raw = item.get("confidence")
            if raw is None:
                continue
            try:
                value = float(raw)
            except Exception:
                continue
            if value > 0:
                values.append(value)
    if not values:
        return None
    return min(values)


class AutoReplyRunner:
    def __init__(
        self,
        *,
        vision_sensor: Any | None = None,
        idle_sensor: Any | None = None,
        ui: Any | None = None,
        llm_client: Any | None = None,
        load_config_fn: Callable[[], dict[str, Any]] = load_config,
        load_state_fn: Callable[[], dict[str, Any]] = load_state,
        save_state_fn: Callable[[dict[str, Any]], None] = save_state,
        append_event_fn: Callable[..., None] = append_event,
        now_fn: Callable[[], float] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.vision = vision_sensor or _VisionSensor()
        self.idle = idle_sensor or _IdleSensor()
        self.ui = ui or wechat_ui
        self.llm = llm_client
        self.load_config = load_config_fn
        self.load_state = load_state_fn
        self.save_state = save_state_fn
        self.append_event = append_event_fn
        self.now = now_fn or time.time
        self.dry_run = dry_run
        self._internal_ui_grace_until = 0.0
        self._internal_ui_idle_floor = 0.0

    def _contact_memory_enabled(self, config: dict[str, Any]) -> bool:
        return bool(config.get("contact_memory_enabled", True))

    def _load_contact_memory(self, config: dict[str, Any], contact: str) -> dict[str, Any]:
        if not self._contact_memory_enabled(config):
            return {}
        return get_contact_memory(contact)

    def _remember_contact_memory(
        self,
        config: dict[str, Any],
        contact: str,
        *,
        context_messages: list[dict[str, str]] | None = None,
        inbound_text: str = "",
        outbound_text: str = "",
        source: str,
    ) -> dict[str, Any]:
        if not self._contact_memory_enabled(config):
            return {}
        memory = remember_contact_memory(
            contact,
            context_messages=context_messages,
            inbound_text=inbound_text,
            outbound_text=outbound_text,
            max_events=int(config.get("contact_memory_max_events", 18) or 18),
            retention_days=int(config.get("contact_memory_retention_days", 14) or 14),
        )
        self.append_event(
            "contact_memory_updated",
            contact=contact,
            source=source,
            recent_summary=str(memory.get("recent_summary") or ""),
            event_count=len(list(memory.get("recent_events") or [])),
        )
        return memory

    def _menu_unread_signal(self) -> str:
        if hasattr(self.vision, "unread_signal"):
            try:
                return str(self.vision.unread_signal() or "")
            except Exception:
                pass
        return "1" if bool(self.vision.check_unread_dot()) else ""

    def _raw_idle_seconds(self) -> float:
        return float(self.idle.get_idle_time_seconds())

    def _live_idle_seconds(self) -> float:
        seconds = self._raw_idle_seconds()
        if self.now() < self._internal_ui_grace_until:
            return max(seconds, self._internal_ui_idle_floor)
        self._internal_ui_idle_floor = 0.0
        return seconds

    def _run_internal_ui_action(
        self,
        action: Callable[..., Any],
        *args: Any,
        grace_seconds: float = 1.5,
        **kwargs: Any,
    ) -> Any:
        idle_floor = max(self._live_idle_seconds(), 30.0)
        result = action(*args, **kwargs)
        # Only suppress our own synthetic input for a very short window.
        # Longer masking makes real user activity look idle and lets the
        # runner keep stealing focus after the user has resumed control.
        effective_grace = min(
            max(float(grace_seconds or 0.0), 0.0),
            MAX_INTERNAL_UI_SUPPRESSION_SECONDS,
        )
        self._internal_ui_idle_floor = max(self._internal_ui_idle_floor, idle_floor)
        if effective_grace > 0:
            self._internal_ui_grace_until = max(
                self._internal_ui_grace_until,
                self.now() + effective_grace,
            )
        return result

    def _activate_wechat_ui(self) -> None:
        self._run_internal_ui_action(self.ui.activate_wechat, grace_seconds=2.5)

    def _probe_ui(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        sleep_after_click = float(kwargs.get("sleep_after_click", 0.0) or 0.0)
        select_chat = kwargs.get("select_chat")
        grace_seconds = 1.5
        if select_chat:
            grace_seconds = max(grace_seconds, sleep_after_click + 1.0)
        return self._run_internal_ui_action(self.ui.probe, *args, grace_seconds=grace_seconds, **kwargs)

    def _probe_roster_background_ui(self) -> dict[str, Any]:
        probe = getattr(self.ui, "probe_roster_background", None)
        if not callable(probe):
            raise RuntimeError("background roster probe unavailable")
        return dict(probe() or {})

    def _focus_input_box_ui(self, selected: dict[str, Any]) -> None:
        self._run_internal_ui_action(self.ui.focus_input_box, selected, grace_seconds=1.0)

    def _paste_text_ui(self, text: str) -> None:
        self._run_internal_ui_action(self.ui.paste_text, text, grace_seconds=1.0)

    def _send_message_ui(self) -> None:
        self._run_internal_ui_action(self.ui.send_message, grace_seconds=1.0)

    def _read_input_box_text_ui(self, selected: dict[str, Any]) -> str:
        return str(
            self._run_internal_ui_action(
                self.ui.read_input_box_text,
                selected,
                grace_seconds=2.0,
            )
            or ""
        ).strip()

    def _hide_wechat_ui(self) -> None:
        self._run_internal_ui_action(self.ui.hide_wechat, grace_seconds=0.75)

    def _restore_frontmost_app_ui(self, app_name: str) -> None:
        self._run_internal_ui_action(self.ui.restore_frontmost_app, app_name, grace_seconds=0.75)

    def _open_wechat_foreground(self, *, reason: str, **event_payload: Any) -> str:
        restore_app = ""
        if hasattr(self.ui, "capture_frontmost_app"):
            try:
                restore_app = str(self.ui.capture_frontmost_app() or "")
            except Exception:
                restore_app = ""
        self.append_event(
            "wechat_window_action",
            action="open",
            reason=reason,
            **event_payload,
        )
        self._activate_wechat_ui()
        return restore_app

    def _close_wechat_foreground(
        self,
        *,
        reason: str,
        restore_app: str,
        **event_payload: Any,
    ) -> None:
        self.append_event(
            "wechat_window_action",
            action="hide",
            reason=reason,
            **event_payload,
        )
        self._hide_wechat_ui()
        if restore_app and hasattr(self.ui, "restore_frontmost_app"):
            try:
                self._restore_frontmost_app_ui(restore_app)
            except Exception as exc:
                self.append_event(
                    "restore_frontmost_failed",
                    context=reason,
                    error=str(exc),
                    app=restore_app,
                )

    def _claim_abort_user_active(
        self,
        state: dict[str, Any],
        *,
        idle_threshold: float,
        phase: str,
    ) -> dict[str, Any]:
        live_idle_seconds = self._live_idle_seconds()
        state["claim_retry_pending"] = True
        self.append_event(
            "claim_aborted_user_active",
            phase=phase,
            idle_seconds=round(live_idle_seconds, 2),
            threshold=idle_threshold,
            queue_contacts=queued_contacts(sync_pending_state(state)),
        )
        return {
            "status": "idle_wait",
            "idle_seconds": round(live_idle_seconds, 2),
            "menu_unread": bool(state.get("last_menu_unread")),
        }

    def _pending_abort_user_active(
        self,
        state: dict[str, Any],
        pending: dict[str, Any],
        *,
        idle_threshold: float,
        phase: str,
    ) -> dict[str, Any]:
        live_idle_seconds = self._live_idle_seconds()
        queue = sync_pending_state(state)
        self.append_event(
            "pending_aborted_user_active",
            contact=str(pending.get("contact", "")).strip(),
            phase=phase,
            idle_seconds=round(live_idle_seconds, 2),
            threshold=idle_threshold,
            queue_length=len(queue),
            queue_contacts=queued_contacts(queue),
        )
        return {
            "status": "pending_wait_user_active",
            "idle_seconds": round(live_idle_seconds, 2),
            "contact": pending.get("contact"),
            "queue_length": len(queue),
        }

    def tick(self) -> dict[str, Any]:
        config = self.load_config()
        state = self.load_state()
        state["last_run_at"] = utc_now_iso()
        now = float(self.now())
        recent_outbound_ttl = _recent_auto_outbound_ttl(config)
        _prune_recent_auto_outbounds(state, now=now, ttl_seconds=recent_outbound_ttl)
        result: dict[str, Any]

        try:
            if not config.get("enabled"):
                state["last_error"] = ""
                result = {"status": "disabled"}
                return result

            idle_seconds = float(self.idle.get_idle_time_seconds())
            idle_threshold = float(config.get("idle_threshold_seconds", 30))
            idle_probe_armed = bool(state.get("idle_probe_armed", True))
            claim_retry_pending = bool(state.get("claim_retry_pending", False))
            if idle_seconds < idle_threshold:
                # While user is active, don't sample menubar digits; clear transient menu signal
                # to avoid stale "has digit" state triggering a late false open.
                state["idle_probe_armed"] = True
                idle_probe_armed = True
                if not claim_retry_pending:
                    state["last_menu_unread"] = False
                    state["last_menu_signal"] = ""
                    state["last_claim_menu_signal"] = ""
                    state["pending_menu_clear_streak"] = 0

            def _record_menu_signal(menu_signal: str, *, source: str) -> None:
                has_unread = bool(menu_signal)
                state["last_menu_unread"] = has_unread
                state["last_menu_signal"] = menu_signal
                state["last_menu_check_at"] = now
                state["idle_probe_armed"] = False
                pending_snapshot = state.get("pending_queue")
                has_pending_snapshot = isinstance(pending_snapshot, list) and bool(pending_snapshot)
                if not has_pending_snapshot and state.get("pending"):
                    has_pending_snapshot = True
                if has_unread:
                    state["pending_menu_clear_streak"] = 0
                else:
                    if not has_pending_snapshot:
                        state["last_claim_menu_signal"] = ""
                        state["pending_menu_clear_streak"] = 0
                    else:
                        streak = int(state.get("pending_menu_clear_streak", 0) or 0) + 1
                        state["pending_menu_clear_streak"] = streak
                        if streak >= 2:
                            state["last_claim_menu_signal"] = ""
                self.append_event("menu_bar_checked", unread=has_unread, signal=menu_signal, source=source)

            menu_checked_now = False
            should_check_menu = False
            if idle_seconds >= idle_threshold:
                check_interval_due = now - float(state.get("last_menu_check_at", 0.0) or 0.0) >= float(
                    config.get("menubar_check_interval_seconds", 15)
                )
                should_check_menu = check_interval_due or idle_probe_armed
            if should_check_menu:
                _record_menu_signal(self._menu_unread_signal(), source="interval")
                menu_checked_now = True

            cleanup_interval = float(config.get("capture_cleanup_interval_seconds", 3600))
            if now - float(state.get("last_capture_cleanup_at", 0.0) or 0.0) >= cleanup_interval:
                state["last_capture_cleanup_at"] = now
                try:
                    cleanup = cleanup_runtime_artifacts_older_than(
                        older_than_seconds=float(config.get("capture_retention_days", 2)) * 24 * 60 * 60,
                        now=now,
                    )
                except Exception as exc:
                    self.append_event("runtime_cleanup_failed", error=str(exc))
                    cleanup = {}
                if int(cleanup.get("deleted_count", 0) or 0) > 0:
                    self.append_event(
                        "runtime_cleanup",
                        deleted_count=int(cleanup.get("deleted_count", 0) or 0),
                        deleted_bytes=int(cleanup.get("deleted_bytes", 0) or 0),
                        captures_deleted=int(cleanup.get("captures_deleted", 0) or 0),
                        debug_deleted=int(cleanup.get("debug_deleted", 0) or 0),
                        logs_deleted=int(cleanup.get("logs_deleted", 0) or 0),
                        events_deleted=int(cleanup.get("events_deleted", 0) or 0),
                        retention_seconds=float(cleanup.get("retention_seconds", 0.0) or 0.0),
                    )

            queue = sync_pending_state(state)
            stale_ttl_seconds = float(config.get("pending_stale_ttl_seconds", 86400))
            queue, stale_removed = prune_stale_pending(queue, now=now, ttl_seconds=stale_ttl_seconds)
            if stale_removed:
                sync_pending_state(state, queue)
                self.append_event(
                    "pending_gc_removed",
                    removed_count=len(stale_removed),
                    removed_contacts=queued_contacts(stale_removed),
                    ttl_seconds=stale_ttl_seconds,
                    queue_length=len(queue),
                    queue_contacts=queued_contacts(queue),
                )
            had_queue = bool(queue)
            # If a pending send is already due, do one immediate signal check first.
            # This makes "new unread while pending" go through claim flow before send-time recheck.
            if (
                queue
                and idle_seconds >= idle_threshold
                and now >= float(queue[0].get("due_at", 0.0) or 0.0)
                and not menu_checked_now
            ):
                _record_menu_signal(self._menu_unread_signal(), source="due_precheck")
                menu_checked_now = True
            current_menu_signal = str(state.get("last_menu_signal") or "")
            actionable_menu_signal = is_actionable_menu_signal(current_menu_signal)
            previous_claim_signal = str(state.get("last_claim_menu_signal") or "")
            menu_signal_rising_now = menu_signal_rising(current_menu_signal, previous_claim_signal)
            roster_sweep_interval = float(config.get("roster_sweep_interval_seconds", 60) or 60)
            roster_sweep_due = now - float(state.get("last_roster_sweep_at", 0.0) or 0.0) >= roster_sweep_interval
            passive_sweep_due = bool(config.get("passive_roster_sweep_enabled", False)) and roster_sweep_due
            passive_claim_ready = False
            if (
                passive_sweep_due
                and not actionable_menu_signal
                and not bool(state.get("claim_retry_pending", False))
            ):
                try:
                    background_probe = self._probe_roster_background_ui()
                    visible_chats = list(background_probe.get("visibleChats", []) or [])
                    allowed_contacts = list(config.get("allowed_contacts", []))
                    preflight = evaluate_passive_preflight(visible_chats, allowed_contacts)
                    state["last_roster_sweep_at"] = now
                    passive_claim_ready = preflight.badge_detected
                    self.append_event(
                        "passive_roster_preflight",
                        badge_detected=preflight.badge_detected,
                        badge_rows=list(preflight.badge_rows),
                        visible_chat_count=preflight.visible_chat_count,
                        screenshot=str(background_probe.get("screenshot") or ""),
                    )
                except Exception as exc:
                    self.append_event(
                        "passive_roster_preflight_failed",
                        error=str(exc),
                        fallback="foreground_claim_scan",
                    )
            claim_decision = decide_claim(
                idle_seconds=idle_seconds,
                idle_threshold=idle_threshold,
                has_queue=bool(queue),
                pending_due=bool(queue and now >= float(queue[0].get("due_at", 0.0) or 0.0)),
                sweep_while_pending=bool(config.get("sweep_while_pending", False)),
                actionable_menu_signal=actionable_menu_signal,
                menu_checked_now=menu_checked_now,
                menu_signal_rising_now=menu_signal_rising_now,
                roster_sweep_due=roster_sweep_due,
                claim_retry_pending=bool(state.get("claim_retry_pending", False)),
                passive_claim_ready=passive_claim_ready,
            )
            claim_result: dict[str, Any] | None = None
            if claim_decision.should_claim:
                state["idle_probe_armed"] = False
                state["last_claim_menu_signal"] = current_menu_signal
                claim_result = self._handle_claim(
                    config,
                    state,
                    idle_seconds,
                    now,
                    claim_trigger=claim_decision.trigger,
                )
                queue = sync_pending_state(state)

            if claim_result and claim_result.get("status") in {"draft_saved", "drafts_saved"} and not had_queue:
                result = claim_result
            elif queue:
                result = self._handle_pending(config, state, queue[0], idle_seconds, now)
            elif claim_result is not None:
                result = claim_result
            else:
                result = {
                    "status": "idle_wait",
                    "idle_seconds": round(idle_seconds, 2),
                    "menu_unread": bool(state.get("last_menu_unread")),
                }
            state["last_error"] = ""
            return result
        except Exception as exc:
            state["last_error"] = str(exc)
            self.append_event("runner_error", error=str(exc))
            return {"status": "error", "reason": str(exc)}
        finally:
            self.save_state(state)

    def _build_llm(self, config: dict[str, Any]) -> Any:
        if self.llm is not None:
            return self.llm
        fallback_client = OllamaClient(
            url=str(config.get("ollama_url")),
            model=str(config.get("ollama_model")),
            max_reply_chars=int(config.get("max_reply_chars", 90)),
            style_instructions=str(config.get("reply_style_instructions", "")),
            emoji_pack_zip_path=str(config.get("emoji_pack_zip_path", "")),
            emoji_enabled=bool(config.get("reply_emoji_enabled", True)),
            emoji_min_count=int(config.get("reply_emoji_min_count", 1)),
            emoji_max_count=int(config.get("reply_emoji_max_count", 2)),
        )
        if not bool(config.get("erge_enabled", True)):
            return fallback_client
        return ErgeClient(
            gateway_url=str(config.get("erge_gateway_url", "")).strip(),
            health_url=str(config.get("erge_health_url", "")).strip(),
            model=str(config.get("erge_model", "brother")).strip() or "brother",
            fallback_client=fallback_client,
            health_timeout_seconds=float(config.get("erge_health_timeout_seconds", 2)),
            health_cache_seconds=float(config.get("erge_health_cache_seconds", 15)),
            request_timeout_seconds=float(config.get("erge_request_timeout_seconds", 120)),
        )

    def _handle_claim(
        self,
        config: dict[str, Any],
        state: dict[str, Any],
        idle_seconds: float,
        now: float,
        *,
        claim_trigger: str = "menu_signal",
    ) -> dict[str, Any]:
        idle_threshold = float(config.get("idle_threshold_seconds", 30))
        live_idle_seconds = self._live_idle_seconds()
        if live_idle_seconds < idle_threshold:
            return self._claim_abort_user_active(state, idle_threshold=idle_threshold, phase="pre_handle")
        # Recheck right before any foreground UI action to avoid stale idle snapshots.
        live_idle_seconds = self._live_idle_seconds()
        if live_idle_seconds < idle_threshold:
            return self._claim_abort_user_active(state, idle_threshold=idle_threshold, phase="pre_open_recheck")
        state["last_roster_sweep_at"] = now
        restore_app = self._open_wechat_foreground(
            reason="claim_scan",
            trigger=claim_trigger,
            queue_contacts=queued_contacts(sync_pending_state(state)),
        )
        try:
            if self._live_idle_seconds() < idle_threshold:
                return self._claim_abort_user_active(state, idle_threshold=idle_threshold, phase="after_open")
            state["claim_retry_pending"] = False
            allowed_contacts = list(config.get("allowed_contacts", []))
            badge_min_frames = max(1, int(config.get("badge_stability_frames", 2) or 2))
            non_whitelist_badge_min_frames = max(
                1, int(config.get("non_whitelist_badge_stability_frames", 1) or 1)
            )
            queue = sync_pending_state(state)
            probe_result = self._probe_ui()
            _update_badge_streaks(state, probe_result, allowed_contacts)

            def _clear_non_whitelist_unread(snapshot: dict[str, Any]) -> list[str]:
                non_whitelist_unread = choose_non_whitelist_unread(snapshot, allowed_contacts)
                unstable_non_whitelist: list[dict[str, Any]] = []
                stable_non_whitelist: list[dict[str, Any]] = []
                for chat in non_whitelist_unread:
                    contact = str(chat.get("name", "")).strip()
                    streak = _contact_badge_streak(state, contact, allowed_contacts)
                    if streak >= non_whitelist_badge_min_frames:
                        stable_non_whitelist.append(chat)
                        continue
                    unstable_non_whitelist.append(
                        {
                            "contact": contact,
                            "streak": streak,
                            "required": non_whitelist_badge_min_frames,
                            "red": int(chat.get("redPixelCount", 0) or 0),
                            "digit": int(chat.get("digitPixelCount", 0) or 0),
                        }
                    )
                if unstable_non_whitelist:
                    self.append_event(
                        "non_whitelist_badge_unstable_skip",
                        details=unstable_non_whitelist,
                        queue_contacts=queued_contacts(queue),
                    )
                non_whitelist_unread = stable_non_whitelist
                cleared_contacts: list[str] = []
                seen_names: set[str] = set()
                for chat in non_whitelist_unread:
                    if self._live_idle_seconds() < idle_threshold:
                        return cleared_contacts
                    contact = str(chat.get("name", "")).strip()
                    key = normalize_text(contact)
                    if not contact or key in seen_names:
                        continue
                    seen_names.add(key)
                    self._probe_ui(
                        select_chat=contact,
                        sleep_after_click=0.25,
                        select_chat_click=chat.get("click"),
                    )
                    cleared_contacts.append(contact)
                if cleared_contacts:
                    self.append_event(
                        "non_whitelist_unread_cleared",
                        contacts=cleared_contacts,
                        queue_contacts=queued_contacts(queue),
                    )
                return cleared_contacts

            def _rescue_whitelist_candidates_from_badge_rows(
                snapshot: dict[str, Any],
                *,
                max_attempts: int = 3,
            ) -> list[dict[str, Any]]:
                rescued: list[dict[str, Any]] = []
                attempts: list[dict[str, Any]] = []
                visible = sorted(
                    list(snapshot.get("visibleChats", []) or []),
                    key=lambda chat: (
                        -1 if _has_row_numeric_unread_badge(chat) else 0,
                        -int(chat.get("digitPixelCount", 0) or 0),
                        -int(chat.get("redPixelCount", 0) or 0),
                        float(chat.get("ocrTop", 0.0) or 0.0),
                    ),
                )
                for chat in visible:
                    if len(attempts) >= max_attempts:
                        break
                    if self._live_idle_seconds() < idle_threshold:
                        break
                    if not _has_row_numeric_unread_badge(chat):
                        continue
                    row_name = str(chat.get("name", "")).strip()
                    if not row_name:
                        continue
                    # Already-resolved whitelist rows should flow through normal candidate path.
                    if _match_allowed_contact(row_name, allowed_contacts):
                        continue
                    click = chat.get("click")
                    if not isinstance(click, dict):
                        continue
                    attempts.append(
                        {
                            "row_name": row_name,
                            "red": int(chat.get("redPixelCount", 0) or 0),
                            "digit": int(chat.get("digitPixelCount", 0) or 0),
                        }
                    )
                    selected = self._probe_ui(
                        select_chat=row_name,
                        sleep_after_click=0.25,
                        select_chat_click=click,
                    )
                    active_chat = str(selected.get("activeChat") or "").strip()
                    matched_contact = _match_allowed_contact(active_chat, allowed_contacts)
                    if not matched_contact:
                        continue
                    selected_visible = find_visible_chat(selected, matched_contact)
                    candidate = copy.deepcopy(selected_visible if selected_visible else chat)
                    candidate["name"] = active_chat or row_name
                    candidate["matchedContact"] = matched_contact
                    candidate["source"] = "title_rescue"
                    candidate["click"] = click
                    rescued.append(candidate)
                    break
                if attempts:
                    self.append_event(
                        "claim_title_rescue_attempt",
                        attempts=attempts,
                        rescued_contacts=[candidate_contact_name(item) for item in rescued],
                        queue_contacts=queued_contacts(queue),
                    )
                return rescued

            candidates = choose_whitelist_candidates(probe_result, allowed_contacts)
            candidates, unstable_candidates = _filter_candidates_by_badge_streak(
                candidates,
                state=state,
                allowed_contacts=allowed_contacts,
                min_frames=badge_min_frames,
            )
            if unstable_candidates:
                self.append_event(
                    "claim_badge_unstable_skip",
                    details=unstable_candidates,
                    queue_contacts=queued_contacts(queue),
                )
            visible_chats = list(probe_result.get("visibleChats", []) or [])
            unread_rows = [
                (
                    f"{str(chat.get('name', '')).strip()}:"
                    f"r{int(chat.get('redPixelCount', 0) or 0)}"
                    f"/d{int(chat.get('digitPixelCount', 0) or 0)}"
                    f"/b{1 if _has_row_numeric_unread_badge(chat) else 0}"
                )
                for chat in visible_chats
                if bool(chat.get("unread")) or int(chat.get("redPixelCount", 0) or 0) > 0
            ]
            row_snapshot = [
                {
                    "name": str(chat.get("name", "")).strip(),
                    "red": int(chat.get("redPixelCount", 0) or 0),
                    "digit": int(chat.get("digitPixelCount", 0) or 0),
                    "numericBadge": bool(chat.get("numericBadge", False)),
                    "unread": bool(chat.get("unread")),
                }
                for chat in visible_chats[:4]
            ]
            self.append_event(
                "claim_candidates",
                contacts=[chat.get("name", "") for chat in candidates],
                visible_unread_rows=unread_rows,
                row_snapshot=row_snapshot,
                trigger=claim_trigger,
                queue_contacts=queued_contacts(queue),
            )
            if not candidates and is_actionable_menu_signal(str(state.get("last_menu_signal") or "")):
                preview_fallback = choose_whitelist_preview_fallback_candidate(
                    probe_result,
                    allowed_contacts,
                    queue=queue,
                    last_seen_inbound=dict(state.get("last_seen_inbound") or {}),
                )
                if preview_fallback:
                    candidates = [preview_fallback]
                    self.append_event(
                        "claim_preview_fallback_candidate",
                        contact=candidate_contact_name(preview_fallback),
                        preview_text=str(preview_fallback.get("preview") or ""),
                        queue_contacts=queued_contacts(queue),
                    )
            if not candidates and is_actionable_menu_signal(str(state.get("last_menu_signal") or "")):
                global_signal_candidates = choose_global_signal_whitelist_probe_candidates(
                    probe_result,
                    allowed_contacts,
                    queue=queue,
                    last_seen_inbound=dict(state.get("last_seen_inbound") or {}),
                    max_search_contacts=int(config.get("global_signal_whitelist_probe_limit", 12) or 12),
                )
                if global_signal_candidates:
                    candidates = global_signal_candidates
                    self.append_event(
                        "claim_global_signal_whitelist_probe_candidates",
                        contacts=[candidate_contact_name(candidate) for candidate in global_signal_candidates],
                        sources=[str(candidate.get("source") or "") for candidate in global_signal_candidates],
                        queue_contacts=queued_contacts(queue),
                    )
            if not candidates and is_actionable_menu_signal(str(state.get("last_menu_signal") or "")):
                active_fallback = choose_active_whitelist_candidate(probe_result, allowed_contacts)
                if active_fallback:
                    candidates = [active_fallback]
                    self.append_event(
                        "claim_active_fallback_candidate",
                        contact=candidate_contact_name(active_fallback),
                        source=str(active_fallback.get("source") or "active_chat_fallback"),
                        preview_text=str(active_fallback.get("preview") or ""),
                        queue_contacts=queued_contacts(queue),
                    )
            elif not candidates and bool(state.get("claim_retry_pending", False)):
                active_fallback = choose_active_whitelist_candidate(probe_result, allowed_contacts)
                if active_fallback:
                    candidates = [active_fallback]
                    self.append_event(
                        "claim_active_fallback_candidate",
                        contact=candidate_contact_name(active_fallback),
                        source=str(active_fallback.get("source") or "active_chat_fallback"),
                        preview_text=str(active_fallback.get("preview") or ""),
                        queue_contacts=queued_contacts(queue),
                    )
            if not candidates:
                state["claim_retry_pending"] = False
                non_whitelist_cleared = bool(
                    claim_trigger != "passive_roster_sweep" and _clear_non_whitelist_unread(probe_result)
                )
                self.append_event(
                    "claim_opened_no_new_message",
                    reason="no_visible_whitelist_unread",
                    signal=str(state.get("last_menu_signal") or ""),
                    trigger=claim_trigger,
                    non_whitelist_cleared=non_whitelist_cleared,
                    visible_chat_count=len(visible_chats),
                    visible_unread_rows=unread_rows,
                    queue_contacts=queued_contacts(queue),
                )
                self.append_event("claim_skipped", reason="no_visible_whitelist_unread")
                if (
                    is_actionable_menu_signal(str(state.get("last_menu_signal") or ""))
                    and not non_whitelist_cleared
                ):
                    self.append_event(
                        "claim_logic_bug",
                        reason="非正常状态栏数字和红点",
                        signal=str(state.get("last_menu_signal") or ""),
                        visible_chat_count=len(list(probe_result.get("visibleChats") or [])),
                        queue_contacts=queued_contacts(queue),
                    )
                return {"status": "no_candidate"}

            added: list[str] = []
            refreshed: list[str] = []
            abort_result: dict[str, Any] | None = None
            queue_fingerprints = {str(item.get("inbound_fingerprint", "")) for item in queue}
            llm = self._build_llm(config)
            recent_outbound_ttl = _recent_auto_outbound_ttl(config)

            def process_candidates(snapshot_candidates: list[dict[str, Any]]) -> None:
                nonlocal queue
                nonlocal queue_fingerprints
                nonlocal abort_result
                for candidate in snapshot_candidates:
                    if self._live_idle_seconds() < idle_threshold:
                        abort_result = self._claim_abort_user_active(
                            state,
                            idle_threshold=idle_threshold,
                            phase="process_candidates",
                        )
                        self.append_event(
                            "claim_aborted_candidate_context",
                            contact=candidate_contact_name(candidate),
                            queue_contacts=queued_contacts(sync_pending_state(state)),
                        )
                        return
                    contact = candidate_contact_name(candidate)
                    existing_index = find_queue_index_for_contact(queue, contact)
                    selected = self._probe_ui(select_chat=contact, select_chat_click=candidate.get("click"))
                    if selected.get("status") != "ok" or not selected.get("selectionConfirmed"):
                        self.append_event("claim_skipped", reason="selection_not_confirmed", contact=contact)
                        continue

                    live_visible = find_visible_chat(selected, contact)
                    if live_visible:
                        if not str(candidate.get("preview") or "").strip():
                            candidate["preview"] = str(live_visible.get("preview") or "")
                        if not str(candidate.get("time") or "").strip():
                            candidate["time"] = str(live_visible.get("time") or "")
                        if bool(live_visible.get("unread")):
                            candidate["unread"] = True
                        for badge_key in ("redPixelCount", "digitPixelCount", "numericBadge", "click"):
                            if badge_key in live_visible and not candidate.get(badge_key):
                                candidate[badge_key] = live_visible.get(badge_key)

                    panel = selected.get("chatPanel", {}) or {}
                    preview_text = str(candidate.get("preview") or "")
                    candidate_source = str(candidate.get("source") or "")
                    if candidate_source.startswith("global_signal_"):
                        preview_confirms = bool(
                            wechat_ui.has_meaningful_text(preview_text)
                            and preview_reliable_for_fallback(preview_text)
                            and not is_history_marker(preview_text)
                        )
                        row_confirms = bool(candidate.get("unread")) or _has_row_numeric_unread_badge(candidate)
                        panel_confirms = latest_panel_bubble_is_inbound_gray(panel)
                        if not (preview_confirms or row_confirms or panel_confirms):
                            self.append_event(
                                "claim_skipped",
                                reason="global_signal_probe_unconfirmed",
                                contact=contact,
                                source=candidate_source,
                            )
                            continue
                    allow_preview_fallback = candidate_source != "active_chat_fallback" or panel_has_claim_signal(panel)
                    outbound_snapshot = latest_committed_outbound(panel)
                    badge_panel_confirms_inbound = bool(
                        _has_row_numeric_unread_badge(candidate)
                        and latest_panel_bubble_is_inbound_gray(panel)
                    )
                    preview_recent_match = _match_recent_auto_outbound(
                        state,
                        contact,
                        preview_text,
                        now=now,
                        ttl_seconds=recent_outbound_ttl,
                    )
                    preview_matches_outbound = _text_matches_outbound(preview_text, outbound_snapshot)
                    if badge_panel_confirms_inbound and (preview_recent_match or preview_matches_outbound):
                        self.append_event(
                            "claim_self_match_overridden_by_inbound_badge",
                            contact=contact,
                            preview_text=preview_text,
                            panel_outbound=outbound_snapshot,
                            recent_auto_match=bool(preview_recent_match),
                        )
                    if preview_recent_match and not badge_panel_confirms_inbound:
                        self.append_event(
                            "claim_skipped_recent_self_preview",
                            contact=contact,
                            preview_text=preview_text,
                            match_mode=str(preview_recent_match.get("match_mode") or ""),
                            source=str(preview_recent_match.get("source") or ""),
                            age_seconds=preview_recent_match.get("age_seconds"),
                        )
                        continue
                    if preview_matches_outbound and not badge_panel_confirms_inbound:
                        if existing_index >= 0:
                            pending_existing = queue[existing_index]
                            self._cancel_pending(
                                state,
                                "manual_reply_detected_preview",
                                pending_existing,
                                current_outbound=outbound_snapshot,
                            )
                            queue = sync_pending_state(state)
                            queue_fingerprints = {str(item.get("inbound_fingerprint", "")) for item in queue}
                        self.append_event(
                            "claim_skipped",
                            reason="preview_matches_outbound",
                            contact=contact,
                        )
                        continue
                    inbound_text = ""
                    if latest_message_is_outbound(panel):
                        # Claim-time stale panel happens in practice; reselect once before skipping.
                        selected_retry = self._probe_ui(
                            select_chat=contact,
                            sleep_after_click=0.45,
                            select_chat_click=candidate.get("click"),
                        )
                        if selected_retry.get("status") == "ok" and selected_retry.get("selectionConfirmed"):
                            selected = selected_retry
                            panel = selected.get("chatPanel", {}) or {}
                            outbound_snapshot = latest_committed_outbound(panel)
                        still_outbound = latest_message_is_outbound(panel)
                        candidate_unread = bool(candidate.get("unread"))
                        inbound_top = latest_meaningful_inbound_top(panel)
                        outbound_item = latest_committed_outbound_item(panel)
                        outbound_top = float(outbound_item.get("top", 0.0) or 0.0) if outbound_item else None
                        outbound_text = str(outbound_item.get("text") or "") if outbound_item else ""
                        self.append_event(
                            "claim_outbound_recheck",
                            contact=contact,
                            still_outbound=still_outbound,
                            candidate_unread=candidate_unread,
                            latest_inbound_top=inbound_top,
                            latest_outbound_top=outbound_top,
                            latest_outbound_right_aligned=_outbound_item_right_aligned(outbound_item),
                            latest_outbound_text=outbound_text,
                        )
                        if still_outbound:
                            preview_fallback = preview_text.strip()
                            if (
                                candidate_unread
                                and wechat_ui.has_meaningful_text(preview_fallback)
                                and not _text_matches_outbound(preview_fallback, outbound_snapshot)
                            ):
                                # Keep the unread claim if panel OCR is stale but row preview is clearly inbound.
                                self.append_event(
                                    "claim_preview_fallback",
                                    reason="latest_outbound_recheck",
                                    contact=contact,
                                    preview_text=preview_fallback,
                                    panel_outbound=outbound_snapshot,
                                )
                                inbound_text = preview_fallback
                            else:
                                self.append_event("claim_skipped", reason="latest_message_outbound", contact=contact)
                                continue
                    inbound_payload = {
                        "text": inbound_text,
                        "quoted_text": "",
                        "quoted_role": "",
                        "quoted_sender": "",
                    }
                    if not inbound_text:
                        inbound_payload = extract_inbound_payload(
                            panel,
                            str(candidate.get("preview") or "") if allow_preview_fallback else "",
                        )
                        inbound_text = str(inbound_payload.get("text") or "").strip()
                    empty_inbound_debug: dict[str, Any] = {}
                    if not inbound_text:
                        if candidate_source == "active_chat_fallback" and not panel_has_claim_signal(panel):
                            self.append_event(
                                "claim_active_fallback_empty_panel",
                                contact=contact,
                                preview_text=preview_text,
                            )
                        # One more hard reselect before giving up; some chat windows
                        # render late and first OCR pass comes back empty.
                        selected_retry = self._probe_ui(
                            select_chat=contact,
                            sleep_after_click=0.65,
                            select_chat_click=candidate.get("click"),
                        )
                        if selected_retry.get("status") == "ok" and selected_retry.get("selectionConfirmed"):
                            selected = selected_retry
                            panel = selected.get("chatPanel", {}) or {}
                            outbound_snapshot = latest_committed_outbound(panel)
                            live_visible = find_visible_chat(selected, contact)
                            allow_retry_preview_fallback = (
                                candidate_source != "active_chat_fallback" or panel_has_claim_signal(panel)
                            )
                            inbound_payload = extract_inbound_payload(
                                panel,
                                str(live_visible.get("preview") or "") if allow_retry_preview_fallback else "",
                            )
                            inbound_text = str(inbound_payload.get("text") or "").strip()
                            self.append_event(
                                "claim_empty_inbound_reselect",
                                contact=contact,
                                has_inbound=bool(inbound_text),
                                live_preview=str(live_visible.get("preview") or ""),
                            )
                    if not inbound_text:
                        empty_inbound_debug = _empty_inbound_debug_payload(
                            contact=contact,
                            now=now,
                            selected=selected,
                            panel=panel,
                            candidate=candidate,
                        )
                        self.append_event("claim_empty_inbound_debug", **empty_inbound_debug)
                    if not inbound_text and bool(candidate.get("unread")):
                        candidate_preview = str(candidate.get("preview") or "").strip()
                        if wechat_ui.has_meaningful_text(candidate_preview):
                            inbound_text = candidate_preview
                            self.append_event(
                                "claim_preview_fallback",
                                reason="empty_inbound_candidate_preview",
                                contact=contact,
                                preview_text=candidate_preview,
                                debug=empty_inbound_debug,
                            )
                    if not inbound_text and bool(candidate.get("unread")):
                        # Last-resort safety net: don't swallow unread claims just
                        # because OCR missed bubble text in this round.
                        inbound_text = "[收到你的消息]"
                        self.append_event(
                            "claim_empty_inbound_placeholder",
                            contact=contact,
                            reason="ocr_missed_unread_payload",
                            debug=empty_inbound_debug,
                        )
                    if not inbound_text:
                        self.append_event(
                            "claim_skipped",
                            reason="empty_inbound",
                            contact=contact,
                            debug=empty_inbound_debug,
                        )
                        continue
                    preview_text = str(candidate.get("preview") or "").strip()
                    inbound_recent_match = _match_recent_auto_outbound(
                        state,
                        contact,
                        inbound_text,
                        now=now,
                        ttl_seconds=recent_outbound_ttl,
                    )
                    if inbound_recent_match and not badge_panel_confirms_inbound:
                        self.append_event(
                            "claim_skipped_recent_self_echo",
                            contact=contact,
                            inbound_text=inbound_text,
                            preview_text=preview_text,
                            match_mode=str(inbound_recent_match.get("match_mode") or ""),
                            source=str(inbound_recent_match.get("source") or ""),
                            age_seconds=inbound_recent_match.get("age_seconds"),
                        )
                        continue
                    if bool(candidate.get("unread")) and is_stale_system_inbound_text(inbound_text):
                        replacement_text = ""
                        if wechat_ui.is_nontext_message(preview_text):
                            normalized_preview = preview_text.replace("［", "[").replace("］", "]").strip()
                            if normalized_preview.startswith("[") and normalized_preview.endswith("]"):
                                replacement_text = normalized_preview
                            else:
                                replacement_text = "[收到你的非文字消息]"
                        elif not preview_text:
                            replacement_text = "[收到你的非文字消息]"
                        if replacement_text:
                            self.append_event(
                                "claim_stale_system_text_replaced",
                                contact=contact,
                                stale_text=inbound_text,
                                replacement_text=replacement_text,
                                preview_text=preview_text,
                            )
                            inbound_text = replacement_text
                    if (
                        wechat_ui.has_meaningful_text(preview_text)
                        and preview_reliable_for_fallback(preview_text)
                        and not _text_matches_outbound(preview_text, outbound_snapshot)
                        and wechat_ui.has_meaningful_text(inbound_text)
                        and not preview_panel_equivalent(preview_text, inbound_text)
                    ):
                        # Prevent cross-contact contamination when chat panel lags behind the selected row.
                        self.append_event(
                            "claim_preview_fallback",
                            reason="panel_preview_mismatch",
                            contact=contact,
                            preview_text=preview_text,
                            panel_inbound=inbound_text,
                        )
                        inbound_text = preview_text
                    if (
                        outbound_snapshot
                        and normalize_text(inbound_text) == normalize_text(outbound_snapshot)
                        and not badge_panel_confirms_inbound
                    ):
                        preview_fallback = preview_text
                        if (
                            wechat_ui.has_meaningful_text(preview_fallback)
                            and preview_reliable_for_fallback(preview_fallback)
                            and not _text_matches_outbound(preview_fallback, outbound_snapshot)
                        ):
                            inbound_text = preview_fallback
                        else:
                            self.append_event(
                                "claim_skipped",
                                reason="inbound_equals_outbound",
                                contact=contact,
                            )
                            continue
                    if str(inbound_payload.get("quoted_text") or "").strip():
                        self.append_event(
                            "claim_quote_detected",
                            contact=contact,
                            inbound_text=inbound_text,
                            quoted_text=str(inbound_payload.get("quoted_text") or "").strip(),
                            quoted_role=str(inbound_payload.get("quoted_role") or "").strip(),
                            quoted_sender=str(inbound_payload.get("quoted_sender") or "").strip(),
                        )

                    context_messages = build_reply_context(
                        panel,
                        inbound_text,
                        max_messages=int(config.get("reply_context_messages", 8)),
                        quoted_message=inbound_payload,
                    )
                    message_time = str(candidate.get("time") or "")
                    inbound_fingerprint = fingerprint(contact, inbound_text, message_time)
                    if existing_index >= 0:
                        existing = queue[existing_index]
                        if inbound_variant_equivalent(
                            str(existing.get("inbound_text", "")),
                            inbound_text,
                            pending_time=str(existing.get("message_time", "")),
                            current_time=message_time,
                        ):
                            self.append_event("claim_skipped", reason="inbound_variant_equivalent", contact=contact)
                            continue
                        if inbound_fingerprint == existing.get("inbound_fingerprint"):
                            self.append_event("claim_skipped", reason="already_queued", contact=contact)
                            continue
                        updated = self._refresh_pending(
                            config,
                            state,
                            queue,
                            existing_index,
                            inbound_text,
                            outbound_snapshot,
                            selected,
                            idle_seconds,
                            now,
                            reason="latest_message_seen",
                            message_time=message_time,
                            llm=llm,
                            context_messages=context_messages,
                            inbound_payload=inbound_payload,
                        )
                        queue_fingerprints.discard(str(existing.get("inbound_fingerprint", "")))
                        queue_fingerprints.add(str(updated.get("inbound_fingerprint", "")))
                        refreshed.append(contact)
                        continue
                    if state.get("last_seen_inbound", {}).get(contact) == inbound_fingerprint:
                        self.append_event("claim_skipped", reason="already_seen", contact=contact)
                        continue
                    if inbound_fingerprint in queue_fingerprints:
                        self.append_event("claim_skipped", reason="already_queued", contact=contact)
                        continue

                    contact_memory = self._load_contact_memory(config, contact)
                    draft_text = llm.generate_reply(
                        contact,
                        inbound_text,
                        conversation_context=context_messages,
                        contact_memory=contact_memory,
                        screenshot_path=_preferred_chat_screenshot(selected),
                        quoted_message=inbound_payload,
                    )
                    pending = {
                        "contact": contact,
                        "inbound_text": inbound_text,
                        "quoted_text": str(inbound_payload.get("quoted_text") or "").strip(),
                        "quoted_role": str(inbound_payload.get("quoted_role") or "").strip(),
                        "quoted_sender": str(inbound_payload.get("quoted_sender") or "").strip(),
                        "message_time": message_time,
                        "inbound_fingerprint": inbound_fingerprint,
                        "draft_text": draft_text,
                        "created_at": now,
                        "due_at": now + float(config.get("send_delay_seconds", 300)),
                        "low_confidence_retries": 0,
                        "outbound_snapshot": outbound_snapshot,
                        "active_chat_title": selected.get("activeChat", ""),
                        "chat_context": context_messages,
                    }
                    queue.append(pending)
                    queue_fingerprints.add(inbound_fingerprint)
                    state.setdefault("last_seen_inbound", {})[contact] = inbound_fingerprint
                    sync_pending_state(state, queue)
                    added.append(contact)
                    self.append_event(
                        "draft_saved_locally",
                        contact=contact,
                        inbound_text=inbound_text,
                        draft_text=draft_text,
                        due_at=pending["due_at"],
                        idle_seconds=round(idle_seconds, 2),
                        context_turns=len(context_messages),
                        queue_length=len(queue),
                        queue_contacts=queued_contacts(queue),
                    )
                    self._remember_contact_memory(
                        config,
                        contact,
                        context_messages=context_messages,
                        inbound_text=inbound_text,
                        source="draft_saved",
                    )

            process_candidates(candidates)
            if abort_result is not None:
                return abort_result
            initial_contacts = {candidate_contact_name(candidate) for candidate in candidates if candidate_contact_name(candidate)}
            follow_up_probe = self._probe_ui()
            _update_badge_streaks(state, follow_up_probe, allowed_contacts)
            follow_up_candidates = [
                candidate
                for candidate in choose_whitelist_candidates(follow_up_probe, allowed_contacts)
                if candidate_contact_name(candidate) not in initial_contacts
            ]
            follow_up_candidates, unstable_follow_up = _filter_candidates_by_badge_streak(
                follow_up_candidates,
                state=state,
                allowed_contacts=allowed_contacts,
                min_frames=badge_min_frames,
            )
            if unstable_follow_up:
                self.append_event(
                    "claim_badge_unstable_skip",
                    phase="follow_up",
                    details=unstable_follow_up,
                    queue_contacts=queued_contacts(queue),
                )
            if follow_up_candidates:
                follow_up_visible = list(follow_up_probe.get("visibleChats", []) or [])
                follow_up_unread_rows = [
                    (
                        f"{str(chat.get('name', '')).strip()}:"
                        f"r{int(chat.get('redPixelCount', 0) or 0)}"
                        f"/d{int(chat.get('digitPixelCount', 0) or 0)}"
                        f"/b{1 if _has_row_numeric_unread_badge(chat) else 0}"
                    )
                    for chat in follow_up_visible
                    if bool(chat.get("unread")) or int(chat.get("redPixelCount", 0) or 0) > 0
                ]
                follow_up_snapshot = [
                    {
                        "name": str(chat.get("name", "")).strip(),
                        "red": int(chat.get("redPixelCount", 0) or 0),
                        "digit": int(chat.get("digitPixelCount", 0) or 0),
                        "numericBadge": bool(chat.get("numericBadge", False)),
                        "unread": bool(chat.get("unread")),
                    }
                    for chat in follow_up_visible[:6]
                ]
                self.append_event(
                    "claim_follow_up_candidates",
                    contacts=[chat.get("name", "") for chat in follow_up_candidates],
                    visible_unread_rows=follow_up_unread_rows,
                    row_snapshot=follow_up_snapshot,
                    queue_contacts=queued_contacts(queue),
                )
                process_candidates(follow_up_candidates)
                if abort_result is not None:
                    return abort_result

            if claim_trigger != "passive_roster_sweep":
                try:
                    _clear_non_whitelist_unread(follow_up_probe)
                except Exception as exc:
                    self.append_event(
                        "non_whitelist_clear_failed",
                        error=str(exc),
                        queue_contacts=queued_contacts(queue),
                    )

            changed = added + refreshed
            state["claim_retry_pending"] = False
            if not changed:
                return {"status": "no_candidate"}
            if len(changed) == 1:
                return {"status": "draft_saved", "contact": changed[0], "queue_length": len(queue)}
            return {"status": "drafts_saved", "contacts": changed, "queue_length": len(queue)}
        finally:
            self._close_wechat_foreground(
                reason="claim_scan",
                restore_app=restore_app,
                trigger=claim_trigger,
            )

    def _refresh_pending(
        self,
        config: dict[str, Any],
        state: dict[str, Any],
        queue: list[dict[str, Any]],
        pending_index: int,
        inbound_text: str,
        outbound_snapshot: str,
        selected: dict[str, Any],
        idle_seconds: float,
        now: float,
        *,
        reason: str,
        message_time: str = "",
        llm: Any | None = None,
        refresh_delay_seconds: float | None = None,
        context_messages: list[dict[str, str]] | None = None,
        inbound_payload: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        existing = queue[pending_index]
        contact = str(existing.get("contact", "")).strip()
        client = llm or self._build_llm(config)
        if context_messages is None:
            panel = selected.get("chatPanel", {}) if isinstance(selected, dict) else {}
            context_messages = build_reply_context(
                panel,
                inbound_text,
                max_messages=int(config.get("reply_context_messages", 8)),
                quoted_message=inbound_payload,
            )
        contact_memory = self._load_contact_memory(config, contact)
        draft_text = client.generate_reply(
            contact,
            inbound_text,
            conversation_context=context_messages,
            contact_memory=contact_memory,
            screenshot_path=_preferred_chat_screenshot(selected),
            quoted_message=inbound_payload,
        )
        delay_seconds = float(
            refresh_delay_seconds
            if refresh_delay_seconds is not None
            else config.get("send_delay_seconds", 300)
        )
        updated = copy.deepcopy(existing)
        updated.update(
            {
                "inbound_text": inbound_text,
                "quoted_text": str((inbound_payload or {}).get("quoted_text") or "").strip(),
                "quoted_role": str((inbound_payload or {}).get("quoted_role") or "").strip(),
                "quoted_sender": str((inbound_payload or {}).get("quoted_sender") or "").strip(),
                "message_time": message_time,
                "inbound_fingerprint": fingerprint(contact, inbound_text, message_time),
                "draft_text": draft_text,
                "created_at": now,
                "due_at": now + delay_seconds,
                "low_confidence_retries": 0,
                "outbound_snapshot": outbound_snapshot,
                "active_chat_title": selected.get("activeChat", ""),
                "chat_context": context_messages,
            }
        )
        queue[pending_index] = updated
        sync_pending_state(state, queue)
        state.setdefault("last_seen_inbound", {})[contact] = str(updated.get("inbound_fingerprint", ""))
        self.append_event(
            "pending_refreshed_latest",
            reason=reason,
            contact=contact,
            previous_inbound=str(existing.get("inbound_text", "")),
            inbound_text=inbound_text,
            quoted_text=str((inbound_payload or {}).get("quoted_text") or "").strip(),
            quoted_role=str((inbound_payload or {}).get("quoted_role") or "").strip(),
            draft_text=draft_text,
            due_at=updated["due_at"],
            idle_seconds=round(idle_seconds, 2),
            context_turns=len(context_messages or []),
            queue_length=len(queue),
            queue_contacts=queued_contacts(queue),
        )
        self._remember_contact_memory(
            config,
            contact,
            context_messages=context_messages,
            inbound_text=inbound_text,
            source="pending_refreshed",
        )
        return updated

    def _cancel_pending(self, state: dict[str, Any], reason: str, pending: dict[str, Any], **extra: Any) -> dict[str, Any]:
        queue = sync_pending_state(state)
        filtered = remove_pending_by_fingerprint(queue, pending)
        sync_pending_state(state, filtered)
        self.append_event(
            "pending_cancelled",
            reason=reason,
            contact=pending.get("contact"),
            remaining_queue=len(filtered),
            queue_contacts=queued_contacts(filtered),
            **extra,
        )
        return {"status": "cancelled", "reason": reason, "contact": pending.get("contact"), "queue_length": len(filtered)}

    def _handle_pending(
        self,
        config: dict[str, Any],
        state: dict[str, Any],
        pending: dict[str, Any],
        idle_seconds: float,
        now: float,
    ) -> dict[str, Any]:
        queue = sync_pending_state(state)
        idle_threshold = float(config.get("idle_threshold_seconds", 30))
        if idle_seconds < idle_threshold:
            return {
                "status": "pending_wait_user_active",
                "idle_seconds": round(idle_seconds, 2),
                "contact": pending.get("contact"),
                "queue_length": len(queue),
            }
        live_idle_seconds = self._live_idle_seconds()
        if live_idle_seconds < idle_threshold:
            self.append_event(
                "pending_deferred_user_active",
                contact=str(pending.get("contact", "")).strip(),
                idle_seconds=round(live_idle_seconds, 2),
                threshold=idle_threshold,
                queue_length=len(queue),
                queue_contacts=queued_contacts(queue),
            )
            return {
                "status": "pending_wait_user_active",
                "idle_seconds": round(live_idle_seconds, 2),
                "contact": pending.get("contact"),
                "queue_length": len(queue),
            }
        if now < float(pending.get("due_at", 0.0)):
            return {
                "status": "pending_wait_delay",
                "seconds_remaining": round(float(pending["due_at"]) - now, 2),
                "contact": pending.get("contact"),
                "queue_length": len(queue),
            }
        # Recheck right before foreground UI action to avoid stale idle snapshots.
        live_idle_seconds = self._live_idle_seconds()
        if live_idle_seconds < idle_threshold:
            self.append_event(
                "pending_deferred_user_active",
                phase="pre_open_recheck",
                contact=str(pending.get("contact", "")).strip(),
                idle_seconds=round(live_idle_seconds, 2),
                threshold=idle_threshold,
                queue_length=len(queue),
                queue_contacts=queued_contacts(queue),
            )
            return {
                "status": "pending_wait_user_active",
                "idle_seconds": round(live_idle_seconds, 2),
                "contact": pending.get("contact"),
                "queue_length": len(queue),
            }

        contact = str(pending.get("contact", "")).strip()
        restore_app = self._open_wechat_foreground(reason="pending_send_due", contact=contact)
        try:
            if self._live_idle_seconds() < idle_threshold:
                return self._pending_abort_user_active(
                    state,
                    pending,
                    idle_threshold=idle_threshold,
                    phase="after_open",
                )
            selected = self._probe_ui(select_chat=contact)
            if selected.get("status") != "ok" or not selected.get("selectionConfirmed"):
                return self._cancel_pending(state, "selection_not_confirmed", pending)

            draft_text = str(pending.get("draft_text", ""))
            recent_outbound_ttl = _recent_auto_outbound_ttl(config)

            def _record_recent_auto_outbound(source: str) -> None:
                entry = _remember_recent_auto_outbound(
                    state,
                    contact,
                    draft_text,
                    now=now,
                    source=source,
                    ttl_seconds=recent_outbound_ttl,
                )
                if entry:
                    self.append_event(
                        "recent_auto_outbound_recorded",
                        contact=contact,
                        source=source,
                        text=draft_text,
                    )

            # For send-time recheck, trust only the right chat panel content.
            # Left roster previews are truncated and can cause false "message changed".
            current_time = str(pending.get("message_time", "") or "")

            def _panel_empty_for_recheck(panel_value: dict[str, Any]) -> bool:
                return not bool(
                    panel_value.get("latestInbound")
                    or panel_value.get("latestOutbound")
                    or panel_value.get("inbound")
                    or panel_value.get("outbound")
                    or panel_value.get("misc")
                )

            def _snapshot_has_recheck_signal(snapshot_value: dict[str, Any]) -> bool:
                panel_value = snapshot_value.get("panel", {}) or {}
                return bool(
                    str(snapshot_value.get("raw_inbound") or "").strip()
                    or str(snapshot_value.get("inbound") or "").strip()
                    or str(snapshot_value.get("outbound") or "").strip()
                    or panel_value.get("latestInbound")
                    or panel_value.get("latestOutbound")
                    or panel_value.get("inbound")
                    or panel_value.get("outbound")
                    or panel_value.get("misc")
                )

            recheck_tail_min_top = float(config.get("recheck_tail_min_top", 0.52))
            recheck_tail_span = float(config.get("recheck_tail_span", 0.28))

            def _read_recheck_snapshot(selected_value: dict[str, Any]) -> dict[str, Any]:
                panel_full = selected_value.get("chatPanel", {}) or {}
                panel_value = panel_tail_slice(panel_full, min_top=recheck_tail_min_top, span=recheck_tail_span)
                # Keep send-time recheck anchored to right-panel content.
                # But if the latest inbound is non-text (sticker/emoji), OCR may only expose
                # that signal in the left roster preview, so allow preview as a non-text-only fallback.
                preview_value = ""
                visible_value = find_visible_chat(selected_value, contact)
                visible_preview = str(visible_value.get("preview") or "").strip()
                if wechat_ui.is_nontext_message(visible_preview):
                    preview_value = visible_preview
                inbound_payload_value = extract_inbound_payload(panel_value, preview_value)
                raw_inbound_value = str(inbound_payload_value.get("text") or "").strip()
                inbound_value = raw_inbound_value
                outbound_item_value = latest_committed_outbound_item(panel_value)
                outbound_value = latest_committed_outbound(panel_value)
                outbound_top_value = (
                    float(outbound_item_value.get("top", 0.0) or 0.0) if outbound_item_value else None
                )
                inbound_top_value = latest_meaningful_inbound_top(panel_value)
                latest_outbound_flag = latest_message_is_outbound(panel_value)
                if outbound_value and (
                    normalize_text(inbound_value) == normalize_text(outbound_value)
                    or normalize_text(inbound_value) == normalize_text(draft_text)
                ):
                    inbound_value = ""
                return {
                    "panel": panel_value,
                    "raw_inbound": raw_inbound_value,
                    "inbound": inbound_value,
                    "inbound_payload": inbound_payload_value,
                    "outbound_item": outbound_item_value,
                    "outbound": outbound_value,
                    "outbound_top": outbound_top_value,
                    "inbound_top": inbound_top_value,
                    "latest_outbound": latest_outbound_flag,
                    "chat_window": bool(selected_value.get("chatWindow") or selected_value.get("selectionConfirmed")),
                    "panel_confidence": panel_tail_confidence(panel_value),
                }

            snapshot = _read_recheck_snapshot(selected)
            panel = snapshot["panel"]
            raw_current_inbound = str(snapshot["raw_inbound"] or "")
            current_inbound = str(snapshot["inbound"] or "")
            current_inbound_payload = snapshot["inbound_payload"] if isinstance(snapshot.get("inbound_payload"), dict) else {}
            current_outbound_item = snapshot["outbound_item"] if isinstance(snapshot["outbound_item"], dict) else None
            current_outbound = str(snapshot["outbound"] or "")
            current_outbound_top = snapshot["outbound_top"]
            latest_inbound_top = snapshot["inbound_top"]
            latest_bubble_outbound = bool(snapshot["latest_outbound"])

            needs_reselect = _panel_empty_for_recheck(panel) or not raw_current_inbound
            if needs_reselect:
                prior_snapshot = copy.deepcopy(snapshot)
                self.append_event(
                    "pending_reselect_empty_panel",
                    contact=contact,
                    reason="empty_panel" if _panel_empty_for_recheck(panel) else "empty_inbound",
                    selection_confirmed=bool(selected.get("selectionConfirmed")),
                )
                if self._live_idle_seconds() < idle_threshold:
                    return self._pending_abort_user_active(
                        state,
                        pending,
                        idle_threshold=idle_threshold,
                        phase="reselect_empty_panel",
                    )
                selected_retry = self._probe_ui(select_chat=contact, sleep_after_click=0.45)
                if selected_retry.get("status") == "ok" and selected_retry.get("selectionConfirmed"):
                    selected = selected_retry
                    retry_snapshot = _read_recheck_snapshot(selected)
                    if _snapshot_has_recheck_signal(prior_snapshot) and not _snapshot_has_recheck_signal(retry_snapshot):
                        snapshot = prior_snapshot
                        self.append_event(
                            "pending_reselect_preserved_prior_snapshot",
                            contact=contact,
                            reason="retry_snapshot_empty",
                            preserved_raw_inbound=str(prior_snapshot.get("raw_inbound") or ""),
                            preserved_outbound=str(prior_snapshot.get("outbound") or ""),
                        )
                    else:
                        snapshot = retry_snapshot
                    panel = snapshot["panel"]
                    raw_current_inbound = str(snapshot["raw_inbound"] or "")
                    current_inbound = str(snapshot["inbound"] or "")
                    current_inbound_payload = snapshot["inbound_payload"] if isinstance(snapshot.get("inbound_payload"), dict) else {}
                    current_outbound_item = (
                        snapshot["outbound_item"] if isinstance(snapshot["outbound_item"], dict) else None
                    )
                    current_outbound = str(snapshot["outbound"] or "")
                    current_outbound_top = snapshot["outbound_top"]
                    latest_inbound_top = snapshot["inbound_top"]
                    latest_bubble_outbound = bool(snapshot["latest_outbound"])
                    self.append_event(
                        "pending_reselect_empty_panel_result",
                        contact=contact,
                        panel_empty=_panel_empty_for_recheck(panel),
                        raw_inbound=raw_current_inbound,
                        has_inbound=bool(current_inbound),
                        has_outbound=bool(current_outbound),
                    )
                else:
                    self.append_event(
                        "pending_reselect_empty_panel_result",
                        contact=contact,
                        status=str(selected_retry.get("status") or ""),
                        selection_confirmed=bool(selected_retry.get("selectionConfirmed")),
                    )

            pending_inbound = str(pending.get("inbound_text", ""))
            pending_time = str(pending.get("message_time", ""))
            send_attempts = int(pending.get("send_attempts", 0) or 0)
            pending_outbound_snapshot = str(pending.get("outbound_snapshot", "") or "")

            def _sanitize_recheck_sample(sample_value: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
                if send_attempts <= 0:
                    return sample_value, []
                sample_inbound = str(sample_value.get("inbound") or "").strip()
                if not sample_inbound:
                    return sample_value, []
                cleaned_inbound, stripped_lines = strip_outbound_contamination(
                    pending_inbound,
                    sample_inbound,
                    draft_text,
                    str(pending.get("draft_text", "") or ""),
                    str(sample_value.get("outbound") or ""),
                    pending_outbound_snapshot,
                )
                if not stripped_lines or normalize_text(cleaned_inbound) == normalize_text(sample_inbound):
                    return sample_value, []
                updated_sample = copy.deepcopy(sample_value)
                updated_sample["inbound"] = cleaned_inbound
                raw_inbound_value = str(updated_sample.get("raw_inbound") or "").strip()
                if normalize_text(raw_inbound_value) == normalize_text(sample_inbound):
                    updated_sample["raw_inbound"] = cleaned_inbound
                return updated_sample, stripped_lines

            vote_frames = max(1, int(float(config.get("recheck_vote_frames", 3) or 3)))
            vote_interval = max(0.05, float(config.get("recheck_vote_interval_seconds", 0.25) or 0.25))
            samples: list[dict[str, Any]] = [snapshot]
            for _ in range(vote_frames - 1):
                if self._live_idle_seconds() < idle_threshold:
                    return self._pending_abort_user_active(
                        state,
                        pending,
                        idle_threshold=idle_threshold,
                        phase="recheck_vote",
                    )
                try:
                    voted_probe = self._probe_ui(select_chat=contact, sleep_after_click=vote_interval)
                except Exception as exc:
                    self.append_event(
                        "pending_recheck_vote_probe_failed",
                        contact=contact,
                        error=str(exc),
                    )
                    break
                if voted_probe.get("status") != "ok" or not voted_probe.get("selectionConfirmed"):
                    continue
                samples.append(_read_recheck_snapshot(voted_probe))

            contamination_events: list[dict[str, Any]] = []
            sanitized_samples: list[dict[str, Any]] = []
            for index, item in enumerate(samples):
                sanitized_item, stripped_lines = _sanitize_recheck_sample(item)
                sanitized_samples.append(sanitized_item)
                if stripped_lines:
                    contamination_events.append(
                        {
                            "index": index,
                            "original_inbound": str(item.get("inbound") or ""),
                            "cleaned_inbound": str(sanitized_item.get("inbound") or ""),
                            "stripped_lines": stripped_lines,
                        }
                    )
            if contamination_events:
                samples = sanitized_samples
                self.append_event(
                    "pending_recheck_outbound_contamination_stripped",
                    contact=contact,
                    send_attempts=send_attempts,
                    frames=len(samples),
                    events=contamination_events,
                )

            consensus = build_recheck_consensus(samples)
            anchor_snapshot = consensus.anchor_snapshot
            panel = anchor_snapshot.get("panel", {}) or {}
            current_outbound_item = (
                anchor_snapshot.get("outbound_item") if isinstance(anchor_snapshot.get("outbound_item"), dict) else None
            )
            current_outbound_top = anchor_snapshot.get("outbound_top")
            latest_inbound_top = anchor_snapshot.get("inbound_top")
            raw_current_inbound = consensus.raw_inbound
            current_inbound = consensus.inbound
            current_outbound = consensus.outbound
            latest_bubble_outbound = consensus.latest_outbound
            has_chat_window = consensus.has_chat_window
            self.append_event(
                "pending_recheck_voted",
                contact=contact,
                frames=consensus.frame_count,
                voted_inbound=current_inbound,
                voted_outbound=current_outbound,
                latest_outbound=latest_bubble_outbound,
            )

            self_echo_match = _match_recent_auto_outbound(
                state,
                contact,
                current_inbound,
                now=now,
                ttl_seconds=recent_outbound_ttl,
                extra_texts=[draft_text] if send_attempts > 0 else None,
            )
            if self_echo_match:
                if send_attempts > 0:
                    remaining = remove_pending_by_fingerprint(queue, pending)
                    sync_pending_state(state, remaining)
                    _record_recent_auto_outbound("auto_sent_self_echo_late")
                    self._remember_contact_memory(
                        config,
                        contact,
                        context_messages=list(pending.get("chat_context") or []),
                        inbound_text=str(pending.get("inbound_text") or ""),
                        outbound_text=draft_text,
                        source="auto_sent_self_echo_late",
                    )
                    self.append_event(
                        "auto_sent",
                        contact=contact,
                        draft_text=draft_text,
                        remaining_queue=len(remaining),
                        queue_contacts=queued_contacts(remaining),
                        confirmation="late_self_echo",
                        match_mode=str(self_echo_match.get("match_mode") or ""),
                        self_echo_inbound=current_inbound,
                    )
                    return {"status": "sent", "contact": contact, "queue_length": len(remaining)}
                return self._cancel_pending(
                    state,
                    "current_inbound_matches_recent_auto_outbound",
                    pending,
                    current_inbound=current_inbound,
                    match_mode=str(self_echo_match.get("match_mode") or ""),
                    source=str(self_echo_match.get("source") or ""),
                    age_seconds=self_echo_match.get("age_seconds"),
                )

            draft_match_mode = _draft_match_mode(draft_text, current_outbound)
            if draft_match_mode and current_outbound:
                if int(pending.get("send_attempts", 0) or 0) > 0:
                    remaining = remove_pending_by_fingerprint(queue, pending)
                    sync_pending_state(state, remaining)
                    _record_recent_auto_outbound("auto_sent_late")
                    final_outbound = str(current_outbound or draft_text or "").strip()
                    self._remember_contact_memory(
                        config,
                        contact,
                        context_messages=list(pending.get("chat_context") or []),
                        inbound_text=str(pending.get("inbound_text") or ""),
                        outbound_text=final_outbound,
                        source="auto_sent_late",
                    )
                    self.append_event(
                        "auto_sent",
                        contact=contact,
                        draft_text=draft_text,
                        remaining_queue=len(remaining),
                        queue_contacts=queued_contacts(remaining),
                        confirmation="late",
                        match_mode=draft_match_mode,
                    )
                    return {"status": "sent", "contact": contact, "queue_length": len(remaining)}
                return self._cancel_pending(state, "reply_already_present", pending)

            ultra_short_inbound_tail = bool(current_inbound and _is_ultra_short_fragment(current_inbound))
            manual_reply = detect_manual_reply_trigger(
                current_inbound=current_inbound,
                current_outbound=current_outbound,
                pending_outbound_snapshot=pending_outbound_snapshot,
                current_outbound_top=current_outbound_top,
                latest_inbound_top=latest_inbound_top,
                latest_bubble_outbound=latest_bubble_outbound,
                ultra_short_inbound_tail=ultra_short_inbound_tail,
            )
            if manual_reply.detected:
                reliable_signal, blockers = _manual_reply_signal_is_reliable(
                    current_outbound=current_outbound,
                    current_inbound="" if ultra_short_inbound_tail else current_inbound,
                    current_outbound_item=current_outbound_item,
                    current_outbound_top=current_outbound_top,
                    latest_inbound_top=None if ultra_short_inbound_tail else latest_inbound_top,
                    latest_bubble_outbound=latest_bubble_outbound,
                    has_chat_window=has_chat_window,
                )
                if reliable_signal:
                    return self._cancel_pending(
                        state,
                        "manual_reply_detected",
                        pending,
                        current_outbound=current_outbound,
                    )
                self.append_event(
                    "pending_manual_reply_ambiguous",
                    contact=contact,
                    trigger=manual_reply.trigger,
                    blockers=blockers,
                    current_inbound=current_inbound,
                    current_outbound=current_outbound,
                    latest_outbound=latest_bubble_outbound,
                    has_chat_window=has_chat_window,
                    outbound_top=current_outbound_top,
                    inbound_top=latest_inbound_top,
                )

            confidence_values = [
                float(item.get("panel_confidence"))
                for item in samples
                if item.get("panel_confidence") is not None
            ]
            min_confidence = min(confidence_values) if confidence_values else None
            confidence_threshold = float(config.get("recheck_min_confidence", 0.58))
            low_confidence = bool(min_confidence is not None and min_confidence < confidence_threshold)
            inbound_norms = [normalize_text(str(item.get("inbound") or "")) for item in samples]
            inbound_norms = [value for value in inbound_norms if value]
            unstable_vote = False
            if len(samples) >= 2 and inbound_norms:
                top_hits = Counter(inbound_norms).most_common(1)[0][1]
                unstable_vote = top_hits < ((len(samples) // 2) + 1)
            if low_confidence or unstable_vote:
                base_delay = float(config.get("recheck_low_confidence_delay_seconds", 60) or 60)
                base_delay = max(180.0, base_delay)
                max_delay = float(config.get("recheck_low_confidence_max_delay_seconds", 900) or 900)
                max_retries = max(1, int(config.get("recheck_low_confidence_max_retries", 4) or 4))
                snooze_seconds = float(config.get("recheck_low_confidence_snooze_seconds", 1800) or 1800)
                retries = int(pending.get("low_confidence_retries", 0) or 0) + 1
                delay_seconds = min(max_delay, base_delay * (2 ** max(0, retries - 1)))
                updated = copy.deepcopy(pending)
                updated["low_confidence_retries"] = retries
                if retries >= max_retries:
                    updated["due_at"] = now + snooze_seconds
                    queue[0] = updated
                    sync_pending_state(state, queue)
                    self.append_event(
                        "pending_recheck_snoozed",
                        contact=contact,
                        retries=retries,
                        max_retries=max_retries,
                        min_confidence=min_confidence,
                        confidence_threshold=confidence_threshold,
                        unstable_vote=unstable_vote,
                        snooze_seconds=snooze_seconds,
                        queue_length=len(queue),
                        queue_contacts=queued_contacts(queue),
                    )
                    return {
                        "status": "pending_recheck_snoozed",
                        "contact": contact,
                        "queue_length": len(queue),
                        "seconds_remaining": round(float(updated["due_at"]) - now, 2),
                    }
                updated["due_at"] = now + delay_seconds
                queue[0] = updated
                sync_pending_state(state, queue)
                self.append_event(
                    "pending_recheck_low_confidence",
                    contact=contact,
                    frames=len(samples),
                    retries=retries,
                    min_confidence=min_confidence,
                    confidence_threshold=confidence_threshold,
                    unstable_vote=unstable_vote,
                    delay_seconds=delay_seconds,
                    queue_length=len(queue),
                    queue_contacts=queued_contacts(queue),
                )
                return {
                    "status": "pending_recheck_low_confidence",
                    "contact": contact,
                    "queue_length": len(queue),
                    "seconds_remaining": round(float(updated["due_at"]) - now, 2),
                }

            if not current_inbound:
                return self._cancel_pending(state, "empty_inbound_recheck", pending)

            if inbound_variant_equivalent(
                pending_inbound,
                current_inbound,
                pending_time=pending_time,
                current_time=current_time,
            ):
                current_inbound = pending_inbound or current_inbound
                current_time = pending_time or current_time

            current_fingerprint = fingerprint(contact, current_inbound, current_time)
            if current_fingerprint != pending.get("inbound_fingerprint"):
                if send_attempts > 0:
                    self.append_event(
                        "pending_message_change_ignored_after_send_attempt",
                        contact=contact,
                        previous_inbound=pending_inbound,
                        current_inbound=current_inbound,
                        pending_time=pending_time,
                        current_time=current_time,
                        previous_fingerprint=str(pending.get("inbound_fingerprint", "")),
                        current_fingerprint=current_fingerprint,
                        send_attempts=send_attempts,
                        queue_length=len(queue),
                        queue_contacts=queued_contacts(queue),
                    )
                    current_inbound = pending_inbound or current_inbound
                    current_time = pending_time or current_time
                    current_fingerprint = str(pending.get("inbound_fingerprint", "")) or fingerprint(
                        contact, current_inbound, current_time
                    )
                else:
                    similarity_threshold = float(config.get("pending_change_similarity_threshold", 0.9) or 0.9)
                    debounce_frames = max(1, int(float(config.get("pending_change_debounce_frames", 3) or 3)))
                    min_votes = max(1, int(float(config.get("pending_change_min_votes", 2) or 2)))

                    expanded_samples: list[dict[str, Any]] = list(samples)
                    if len(expanded_samples) < debounce_frames:
                        for _ in range(debounce_frames - len(expanded_samples)):
                            try:
                                voted_probe = self._probe_ui(select_chat=contact, sleep_after_click=vote_interval)
                            except Exception as exc:
                                self.append_event(
                                    "pending_change_vote_probe_failed",
                                    contact=contact,
                                    error=str(exc),
                                )
                                break
                            if voted_probe.get("status") != "ok" or not voted_probe.get("selectionConfirmed"):
                                continue
                            voted_snapshot = _read_recheck_snapshot(voted_probe)
                            original_vote_inbound = str(voted_snapshot.get("inbound") or "")
                            voted_snapshot, stripped_lines = _sanitize_recheck_sample(voted_snapshot)
                            if stripped_lines:
                                self.append_event(
                                    "pending_change_outbound_contamination_stripped",
                                    contact=contact,
                                    send_attempts=send_attempts,
                                    original_inbound=original_vote_inbound,
                                    cleaned_inbound=str(voted_snapshot.get("inbound") or ""),
                                    stripped_lines=stripped_lines,
                                )
                            expanded_samples.append(voted_snapshot)

                    changed_candidates: list[str] = []
                    equivalent_votes = 0
                    similarity_suppressed_votes = 0
                    max_similarity = 0.0
                    for item in expanded_samples:
                        sample_inbound = str(item.get("inbound") or "")
                        if not sample_inbound:
                            continue
                        if inbound_variant_equivalent(
                            pending_inbound,
                            sample_inbound,
                            pending_time=pending_time,
                            current_time=current_time,
                        ):
                            equivalent_votes += 1
                            continue
                        similarity = _message_similarity_score(pending_inbound, sample_inbound)
                        max_similarity = max(max_similarity, similarity)
                        if similarity >= similarity_threshold:
                            similarity_suppressed_votes += 1
                            continue
                        changed_candidates.append(sample_inbound)

                    changed_vote_count = len(changed_candidates)
                    stable_changed_votes = 0
                    stable_changed_inbound = ""
                    if changed_candidates:
                        changed_norms = [normalize_text(value) for value in changed_candidates if normalize_text(value)]
                        if changed_norms:
                            stable_norm, stable_changed_votes = Counter(changed_norms).most_common(1)[0]
                            stable_raws = [value for value in changed_candidates if normalize_text(value) == stable_norm]
                            if stable_raws:
                                stable_changed_inbound = max(stable_raws, key=len)

                    change_reliable = changed_vote_count >= min_votes and stable_changed_votes >= min_votes
                    if not change_reliable:
                        self.append_event(
                            "pending_message_change_suppressed",
                            contact=contact,
                            previous_inbound=pending_inbound,
                            current_inbound=current_inbound,
                            frames=len(expanded_samples),
                            min_votes=min_votes,
                            changed_votes=changed_vote_count,
                            stable_changed_votes=stable_changed_votes,
                            equivalent_votes=equivalent_votes,
                            similarity_suppressed_votes=similarity_suppressed_votes,
                            similarity_threshold=similarity_threshold,
                            max_similarity=round(max_similarity, 4),
                            queue_length=len(queue),
                            queue_contacts=queued_contacts(queue),
                        )
                        current_inbound = pending_inbound or current_inbound
                        current_time = pending_time or current_time
                        current_fingerprint = str(pending.get("inbound_fingerprint", "")) or fingerprint(
                            contact, current_inbound, current_time
                        )
                    else:
                        if stable_changed_inbound:
                            current_inbound = stable_changed_inbound
                        current_fingerprint = fingerprint(contact, current_inbound, current_time)

            if current_fingerprint != pending.get("inbound_fingerprint"):
                refresh_delay_seconds = float(config.get("pending_refresh_delay_seconds", 180))
                self.append_event(
                    "pending_message_changed_recheck",
                    contact=contact,
                    previous_inbound=pending_inbound,
                    current_inbound=current_inbound,
                    pending_time=pending_time,
                    current_time=current_time,
                    previous_fingerprint=str(pending.get("inbound_fingerprint", "")),
                    current_fingerprint=current_fingerprint,
                    truncation_like=truncation_like_change(pending_inbound, current_inbound),
                    delay_seconds=refresh_delay_seconds,
                    frames=len(expanded_samples),
                    queue_length=len(queue),
                    queue_contacts=queued_contacts(queue),
                )
                updated = self._refresh_pending(
                    config,
                    state,
                    queue,
                    0,
                    current_inbound,
                    current_outbound,
                    selected,
                    idle_seconds,
                    now,
                    reason="message_changed",
                    message_time=current_time,
                    refresh_delay_seconds=refresh_delay_seconds,
                    inbound_payload=current_inbound_payload,
                )
                return {
                    "status": "pending_refreshed",
                    "contact": contact,
                    "queue_length": len(queue),
                    "seconds_remaining": round(float(updated["due_at"]) - now, 2),
                }

            if self.dry_run:
                remaining = remove_pending_by_fingerprint(queue, pending)
                sync_pending_state(state, remaining)
                self.append_event(
                    "dry_run_would_send",
                    contact=contact,
                    draft_text=draft_text,
                    remaining_queue=len(remaining),
                    queue_contacts=queued_contacts(remaining),
                )
                return {"status": "dry_run_sent", "contact": contact, "queue_length": len(remaining)}

            manual_input = ""
            retry_input_match_mode = ""
            if hasattr(self.ui, "read_input_box_text"):
                if self._live_idle_seconds() < idle_threshold:
                    return self._pending_abort_user_active(
                        state,
                        pending,
                        idle_threshold=idle_threshold,
                        phase="pre_input_probe",
                    )
                try:
                    manual_input = self._read_input_box_text_ui(selected)
                except Exception as exc:
                    return self._cancel_pending(state, "input_box_probe_failed", pending, error=str(exc))
                if manual_input:
                    retry_input_match_mode = _draft_match_mode(draft_text, manual_input)
            if manual_input and not (send_attempts > 0 and retry_input_match_mode):
                return self._cancel_pending(
                    state,
                    "input_box_modified",
                    pending,
                    input_text=manual_input,
                )

            if self._live_idle_seconds() < idle_threshold:
                return self._pending_abort_user_active(
                    state,
                    pending,
                    idle_threshold=idle_threshold,
                    phase="pre_send",
                )
            confirmed = None
            confirmation_probe_reason = "initial_send"
            if send_attempts > 0:
                if retry_input_match_mode:
                    self.append_event(
                        "send_retry_resend_from_input_buffer",
                        contact=contact,
                        draft_text=draft_text,
                        buffered_text=manual_input,
                        match_mode=retry_input_match_mode,
                        send_attempts=send_attempts,
                    )
                    self._focus_input_box_ui(selected)
                    if self._live_idle_seconds() < idle_threshold:
                        return self._pending_abort_user_active(
                            state,
                            pending,
                            idle_threshold=idle_threshold,
                            phase="retry_after_focus_input",
                        )
                    self._send_message_ui()
                    _record_recent_auto_outbound("send_attempt_retry")
                    confirmed = self._probe_ui(select_chat=contact, sleep_after_click=0.4)
                    confirmation_probe_reason = "retry_resend"
                else:
                    self.append_event(
                        "send_confirmation_retry_without_resend",
                        contact=contact,
                        draft_text=draft_text,
                        send_attempts=send_attempts,
                    )
                    confirmed = self._probe_ui(select_chat=contact, sleep_after_click=0.55)
                    confirmation_probe_reason = "retry_confirm_only"
            else:
                self._focus_input_box_ui(selected)
                if self._live_idle_seconds() < idle_threshold:
                    return self._pending_abort_user_active(
                        state,
                        pending,
                        idle_threshold=idle_threshold,
                        phase="after_focus_input",
                    )
                self._paste_text_ui(draft_text)
                if self._live_idle_seconds() < idle_threshold:
                    return self._pending_abort_user_active(
                        state,
                        pending,
                        idle_threshold=idle_threshold,
                        phase="after_paste",
                    )
                self._send_message_ui()
                _record_recent_auto_outbound("send_attempt")
                confirmed = self._probe_ui(select_chat=contact, sleep_after_click=0.4)
            confirmed_panel = confirmed.get("chatPanel", {}) or {}
            confirmed_outbound = latest_committed_outbound(confirmed_panel)
            confirmed_match_mode = _draft_match_mode(draft_text, confirmed_outbound)
            if confirmed_match_mode and confirmed_outbound:
                remaining = remove_pending_by_fingerprint(queue, pending)
                sync_pending_state(state, remaining)
                _record_recent_auto_outbound("auto_sent_immediate")
                final_outbound = str(confirmed_outbound or draft_text or "").strip()
                self._remember_contact_memory(
                    config,
                    contact,
                    context_messages=list(pending.get("chat_context") or []),
                    inbound_text=str(pending.get("inbound_text") or ""),
                    outbound_text=final_outbound,
                    source="auto_sent_immediate",
                )
                self.append_event(
                    "auto_sent",
                    contact=contact,
                    draft_text=draft_text,
                    remaining_queue=len(remaining),
                    queue_contacts=queued_contacts(remaining),
                    confirmation=confirmation_probe_reason,
                    match_mode=confirmed_match_mode,
                )
                return {"status": "sent", "contact": contact, "queue_length": len(remaining)}

            confirmed_inbound_item = latest_meaningful_inbound_item(confirmed_panel)
            confirmed_inbound = str(
                confirmed_inbound_item.get("text")
                or confirmed_panel.get("latestInbound")
                or ""
            ).strip()
            confirmed_self_echo_match = _match_recent_auto_outbound(
                state,
                contact,
                confirmed_inbound,
                now=now,
                ttl_seconds=recent_outbound_ttl,
                extra_texts=[draft_text],
            )
            if confirmed_self_echo_match:
                remaining = remove_pending_by_fingerprint(queue, pending)
                sync_pending_state(state, remaining)
                _record_recent_auto_outbound("auto_sent_self_echo_immediate")
                self._remember_contact_memory(
                    config,
                    contact,
                    context_messages=list(pending.get("chat_context") or []),
                    inbound_text=str(pending.get("inbound_text") or ""),
                    outbound_text=draft_text,
                    source="auto_sent_self_echo_immediate",
                )
                self.append_event(
                    "auto_sent",
                    contact=contact,
                    draft_text=draft_text,
                    remaining_queue=len(remaining),
                    queue_contacts=queued_contacts(remaining),
                    confirmation="self_echo_after_send",
                    match_mode=str(confirmed_self_echo_match.get("match_mode") or ""),
                    self_echo_inbound=confirmed_inbound,
                    current_outbound=confirmed_outbound,
                )
                return {"status": "sent", "contact": contact, "queue_length": len(remaining)}

            updated = copy.deepcopy(pending)
            updated["send_attempts"] = int(pending.get("send_attempts", 0) or 0) + 1
            updated["last_send_attempt_at"] = now
            updated["due_at"] = now + float(config.get("send_verify_retry_seconds", 45))
            queue[0] = updated
            sync_pending_state(state, queue)
            if updated["send_attempts"] >= int(config.get("send_max_attempts", 2)):
                return self._cancel_pending(
                    state,
                    "send_not_confirmed",
                    updated,
                    current_outbound=confirmed_outbound,
                    send_attempts=updated["send_attempts"],
                )
            self.append_event(
                "send_unconfirmed_retry_scheduled",
                contact=contact,
                draft_text=draft_text,
                current_outbound=confirmed_outbound,
                send_attempts=updated["send_attempts"],
                due_at=updated["due_at"],
                queue_length=len(queue),
                queue_contacts=queued_contacts(queue),
            )
            return {
                "status": "send_unconfirmed_retry",
                "contact": contact,
                "queue_length": len(queue),
                "send_attempts": updated["send_attempts"],
            }
        finally:
            self._close_wechat_foreground(
                reason="pending_send_due",
                restore_app=restore_app,
                contact=contact,
            )


class _VisionSensor:
    def unread_signal(self) -> str:
        return str(unread_signal() or "")

    def check_unread_dot(self) -> bool:
        return bool(check_unread_dot())


class _IdleSensor:
    def get_idle_time_seconds(self) -> float:
        return float(get_idle_time_seconds())
