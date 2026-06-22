from __future__ import annotations

import copy
from dataclasses import dataclass
import re
from typing import Any

from . import wechat_ui


CONTACT_DAY_SUFFIX_RE = re.compile(
    r"(?i)\b(?:today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|wed|thu|fri|sat|sun)\b.*$|(?:今天|昨天|前天).*$"
)
CONTACT_TIME_SUFFIX_RE = re.compile(r"\b\d{1,2}:\d{2}\b$")
CONTACT_TRAILING_COUNT_RE = re.compile(r"\(\s*\d+\s*\)\s*$")

CLAIM_TRIGGER_MENU = "menu_signal"
CLAIM_TRIGGER_RETRY = "claim_retry"
CLAIM_TRIGGER_PASSIVE = "passive_roster_sweep"


def candidate_contact_name(candidate: dict[str, Any]) -> str:
    return str(candidate.get("matchedContact") or candidate.get("name", "")).strip()


def canonical_contact_match_key(text: str) -> str:
    value = " ".join(str(text or "").strip().split())
    if not value:
        return ""
    value = CONTACT_TRAILING_COUNT_RE.sub("", value).strip()
    for _ in range(3):
        before = value
        value = CONTACT_DAY_SUFFIX_RE.sub("", value).strip()
        value = CONTACT_TIME_SUFFIX_RE.sub("", value).strip()
        if value == before:
            break
    value = re.sub(r"^[^0-9A-Za-z\u4e00-\u9fff]+|[^0-9A-Za-z\u4e00-\u9fff]+$", "", value).strip()
    return " ".join(wechat_ui.normalize_name_for_match(value).split())


def match_allowed_contact(candidate_name: str, allowed_contacts: list[str]) -> str:
    candidate_key = canonical_contact_match_key(candidate_name)
    if not candidate_key:
        return ""
    for allowed in allowed_contacts:
        if candidate_key == canonical_contact_match_key(allowed):
            return allowed
    for allowed in allowed_contacts:
        if wechat_ui.names_match(candidate_name, allowed):
            return allowed
    return ""


def has_row_numeric_unread_badge(chat: dict[str, Any]) -> bool:
    if not isinstance(chat, dict) or not bool(chat.get("numericBadge", False)):
        return False
    digit_pixels = int(chat.get("digitPixelCount", 0) or 0)
    red_pixels = max(1, int(chat.get("redPixelCount", 0) or 0))
    if digit_pixels >= 10:
        return True
    digit_ratio = digit_pixels / red_pixels
    if digit_pixels >= 8:
        return digit_ratio >= 0.045
    if digit_pixels >= 6 and red_pixels <= 80:
        return digit_ratio >= 0.11
    return False


def read_badge_streaks(state: dict[str, Any]) -> dict[str, int]:
    raw = state.get("badge_streaks")
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, int] = {}
    for key, value in raw.items():
        token = canonical_contact_match_key(str(key or ""))
        if not token:
            continue
        try:
            streak = int(value or 0)
        except Exception:
            streak = 0
        if streak > 0:
            cleaned[token] = streak
    return cleaned


def badge_streak_key(contact_name: str, allowed_contacts: list[str]) -> str:
    matched = match_allowed_contact(contact_name, allowed_contacts) or str(contact_name or "")
    return canonical_contact_match_key(matched)


def update_badge_streaks(
    state: dict[str, Any],
    probe_result: dict[str, Any],
    allowed_contacts: list[str],
) -> dict[str, int]:
    previous = read_badge_streaks(state)
    updated: dict[str, int] = {}
    for chat in list(probe_result.get("visibleChats", []) or []):
        if not has_row_numeric_unread_badge(chat):
            continue
        key = badge_streak_key(str(chat.get("name", "")), allowed_contacts)
        if not key:
            continue
        updated[key] = min(9, int(previous.get(key, 0) or 0) + 1)
    state["badge_streaks"] = updated
    return updated


def contact_badge_streak(state: dict[str, Any], contact_name: str, allowed_contacts: list[str]) -> int:
    key = badge_streak_key(contact_name, allowed_contacts)
    if not key:
        return 0
    return int(read_badge_streaks(state).get(key, 0) or 0)


def filter_candidates_by_badge_streak(
    candidates: list[dict[str, Any]],
    *,
    state: dict[str, Any],
    allowed_contacts: list[str],
    min_frames: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if min_frames <= 1:
        return candidates, []
    stable: list[dict[str, Any]] = []
    unstable: list[dict[str, Any]] = []
    for candidate in candidates:
        contact = candidate_contact_name(candidate)
        digit_pixels = int(candidate.get("digitPixelCount", 0) or 0)
        if digit_pixels >= 10:
            stable.append(candidate)
            continue
        streak = contact_badge_streak(state, contact, allowed_contacts)
        if streak >= min_frames:
            stable.append(candidate)
            continue
        unstable.append(
            {
                "contact": contact,
                "streak": streak,
                "required": min_frames,
                "red": int(candidate.get("redPixelCount", 0) or 0),
                "digit": digit_pixels,
            }
        )
    return stable, unstable


def choose_whitelist_candidates(
    probe_result: dict[str, Any],
    allowed_contacts: list[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for chat in probe_result.get("visibleChats", []):
        if not has_row_numeric_unread_badge(chat):
            continue
        matched_contact = match_allowed_contact(str(chat.get("name", "")), allowed_contacts)
        if not matched_contact:
            continue
        candidate = copy.deepcopy(chat)
        candidate["matchedContact"] = matched_contact
        candidates.append(candidate)
    return candidates


def choose_non_whitelist_unread(
    probe_result: dict[str, Any],
    allowed_contacts: list[str],
) -> list[dict[str, Any]]:
    unread: list[dict[str, Any]] = []
    for chat in probe_result.get("visibleChats", []):
        if not has_row_numeric_unread_badge(chat):
            continue
        if match_allowed_contact(str(chat.get("name", "")), allowed_contacts):
            continue
        unread.append(copy.deepcopy(chat))
    return unread


@dataclass(frozen=True)
class PassivePreflightResult:
    badge_rows: tuple[dict[str, Any], ...]
    visible_chat_count: int

    @property
    def badge_detected(self) -> bool:
        return bool(self.badge_rows)


def evaluate_passive_preflight(
    visible_chats: list[dict[str, Any]],
    allowed_contacts: list[str],
) -> PassivePreflightResult:
    badge_rows = tuple(
        {
            "name": str(chat.get("name", "")).strip(),
            "red": int(chat.get("redPixelCount", 0) or 0),
            "digit": int(chat.get("digitPixelCount", 0) or 0),
        }
        for chat in visible_chats
        if has_row_numeric_unread_badge(chat)
        and match_allowed_contact(str(chat.get("name", "")), allowed_contacts)
    )
    return PassivePreflightResult(
        badge_rows=badge_rows,
        visible_chat_count=len(visible_chats),
    )


def menu_signal_rising(current_signal: str, previous_signal: str) -> bool:
    current = str(current_signal or "").strip()
    previous = str(previous_signal or "").strip()
    current_actionable = current.isdigit() and int(current) > 0
    previous_actionable = previous.isdigit() and int(previous) > 0
    if not current_actionable:
        return False
    if not previous_actionable:
        return True
    return int(current) > int(previous)


@dataclass(frozen=True)
class ClaimDecision:
    should_claim: bool
    trigger: str
    pending_allowed: bool
    reason: str


def decide_claim(
    *,
    idle_seconds: float,
    idle_threshold: float,
    has_queue: bool,
    pending_due: bool,
    sweep_while_pending: bool,
    actionable_menu_signal: bool,
    menu_checked_now: bool,
    menu_signal_rising_now: bool,
    roster_sweep_due: bool,
    claim_retry_pending: bool,
    passive_claim_ready: bool,
) -> ClaimDecision:
    pending_allowed = bool(
        not has_queue
        or sweep_while_pending
        or pending_due
        or menu_signal_rising_now
    )
    if idle_seconds < idle_threshold:
        return ClaimDecision(False, "", pending_allowed, "user_active")
    if not pending_allowed:
        return ClaimDecision(False, "", False, "pending_queue_blocks_claim")
    if claim_retry_pending:
        return ClaimDecision(True, CLAIM_TRIGGER_RETRY, True, "claim_retry")

    menu_claim_ready = bool(
        actionable_menu_signal
        and (menu_checked_now or passive_claim_ready)
        and (menu_signal_rising_now or passive_claim_ready)
    )
    if menu_claim_ready:
        return ClaimDecision(True, CLAIM_TRIGGER_MENU, True, "menu_signal")
    if passive_claim_ready:
        return ClaimDecision(True, CLAIM_TRIGGER_PASSIVE, True, "passive_badge")
    return ClaimDecision(False, "", True, "no_claim_evidence")
