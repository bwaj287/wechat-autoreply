"""Claim a verified inbound from already rendered chat surfaces without UI actions."""

from __future__ import annotations

from typing import Any

from . import wechat_ui
from .claim_policy import has_row_numeric_unread_badge, match_allowed_contact
from .pending_queue import find_queue_index_for_contact, get_pending_queue


def _rows_for_contact(roster: dict, contact: str) -> list[dict]:
    return [
        row for row in roster.get("visibleChats") or []
        if str(row.get("name") or "").strip() == contact
    ]


def _preview_matches(preview: str, text: str) -> bool:
    from .orchestrator import normalize_text, preview_reliable_for_fallback

    preview, text = normalize_text(preview), normalize_text(text)
    if preview == text:
        return bool(text)
    for ending in ("…", "..."):
        if preview.endswith(ending):
            prefix = preview.removesuffix(ending).rstrip("…").strip()
            return preview_reliable_for_fallback(prefix) and text.startswith(prefix)
    return False


def _confirmed_payload(selected: dict, contact: str, preview: str, message_time: str) -> dict:
    from .orchestrator import (
        extract_inbound_payload, is_stale_system_inbound_text, latest_bubble_is_unresolved,
        latest_meaningful_inbound_item, latest_panel_bubble_is_inbound_gray, normalize_text,
    )

    if selected.get("status") != "ok" or not selected.get("selectionConfirmed"):
        return {}
    panel = selected.get("chatPanel") or {}
    if selected.get("activeChat") != contact or panel.get("title") != contact:
        return {}
    rows = _rows_for_contact(selected, contact)
    if len(rows) != 1 or not has_row_numeric_unread_badge(rows[0]):
        return {}
    if normalize_text(rows[0].get("preview")) != normalize_text(preview) or rows[0].get("time") != message_time:
        return {}
    if latest_bubble_is_unresolved(panel) or not latest_panel_bubble_is_inbound_gray(panel):
        return {}
    latest = latest_meaningful_inbound_item(panel)
    if latest.get("messageKind") == "media" or not _preview_matches(preview, latest.get("text", "")):
        return {}
    payload = extract_inbound_payload(panel)
    if not payload.get("text") or is_stale_system_inbound_text(payload["text"]):
        return {}
    return payload


def process_background_claims(runner, config: dict, state: dict, roster: dict, now: float, *, dock_signal: str = "") -> dict:
    """A Dock count schedules a read; only changing contact-specific evidence can establish freshness."""
    from .orchestrator import fingerprint, latest_committed_outbound

    queue = get_pending_queue(state)
    allowed = list(config.get("allowed_contacts") or [])
    opportunities = state.setdefault("background_claim_opportunities", {})
    for contact in set(opportunities).difference(allowed):
        del opportunities[contact]
    changes = 0
    if roster.get("status") == "ok":
        previous = state.get("background_roster_rows") or {}
        current: dict[str, dict[str, Any]] = {}
        groups: dict[str, list[dict]] = {}
        for row in roster.get("visibleChats") or []:
            contact = str(row.get("name") or "").strip()
            if contact in allowed:
                groups.setdefault(contact, []).append(row)
            elif has_row_numeric_unread_badge(row) and match_allowed_contact(contact, allowed):
                state["foreground_scan_pending"] = True
        max_gap = 2 * max(1, float(config.get("roster_sweep_interval_seconds", 60)))
        for contact, rows in groups.items():
            if len(rows) != 1:
                if any(has_row_numeric_unread_badge(row) for row in rows):
                    state["foreground_scan_pending"] = True
                continue
            row = rows[0]
            preview = str(row.get("preview") or "").strip()
            message_time = str(row.get("time") or "").strip()
            unread = has_row_numeric_unread_badge(row)
            signature = fingerprint(contact, preview, message_time)
            observation = {"signature": signature, "unread": unread, "seen_at": now}
            current[contact] = observation
            if not unread:
                continue
            old = previous.get(contact) or {}
            same_badge = old.get("unread") and old.get("signature") == signature
            if same_badge and old.get("settled"):
                observation["settled"] = True
                continue
            opportunity = opportunities.get(contact) or {}
            recent = bool(old) and 0 <= now - float(old.get("seen_at", 0)) <= max_gap
            changed = recent and old.get("signature") != signature
            if opportunity.get("signature") == signature:
                age = now - float(opportunity.get("seen_at", now))
                changed = changed or (recent and 0 <= age <= max_gap and opportunity.get("observed_change", False))
            opportunities[contact] = {
                "contact": contact, "preview": preview, "time": message_time, "unread": True,
                "signature": signature, "observed_change": bool(changed),
                "seen_at": opportunity.get("seen_at", now) if opportunity.get("signature") == signature else now,
            }
            # Identical screenshots may both show the same cached window surface.
            if not changed or not preview or not message_time or wechat_ui.is_nontext_message(preview):
                continue
            if not wechat_ui.has_meaningful_text(preview):
                continue
            index = find_queue_index_for_contact(queue, contact)
            if index >= 0 and queue[index].get("send_attempts"):
                continue
            probe = getattr(runner.ui, "probe_chat_background", None)
            if not callable(probe):
                continue
            try:
                selected = probe(contact)
                payload = _confirmed_payload(selected, contact, preview, message_time)
                if not payload:
                    continue
                change = runner._record_confirmed_claim(
                    config, state, dict(row, matchedContact=contact), selected, payload, payload["text"],
                    latest_committed_outbound(selected["chatPanel"]), runner._live_idle_seconds(), now,
                )
            except Exception as exc:
                runner.append_event("background_claim_failed", contact=contact, error=type(exc).__name__)
                continue
            changes += bool(change)
            del opportunities[contact]
            observation["settled"] = True
        state["background_roster_rows"] = current
    return {
        "status": "background_draft_saved" if changes else "background_wait",
        "draft_count": changes, "queue_length": len(get_pending_queue(state)),
        "unresolved_count": len(opportunities),
    }
