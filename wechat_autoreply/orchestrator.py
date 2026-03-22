from __future__ import annotations

import copy
import hashlib
import re
import time
from typing import Any, Callable

from .capture_cleanup import delete_capture_snapshots_older_than
from .config_store import load_config
from .event_log import append_event
from .idle import get_idle_time_seconds
from .ollama_client import OllamaClient
from .state_store import load_state, save_state, utc_now_iso
from .vision import check_unread_dot, unread_signal
from . import wechat_ui

HISTORY_MARKER_RE = re.compile(
    r"(?i)^(?:(?:today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|wed|thu|fri|sat|sun|今天|昨天|前天)\s+)?\d{1,2}:\d{2}$"
)


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


def _meaningful_inbound_items(panel: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in list(panel.get("inbound") or []):
        text = str(item.get("text") or "").strip()
        if not wechat_ui.has_meaningful_text(text) or is_history_marker(text):
            continue
        items.append({"text": text, "top": float(item.get("top", 0.0) or 0.0)})
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
    preview_value = str(preview or "").strip()
    if preview_value:
        preview_norm = normalize_text(preview_value)
        if preview_norm == normalize_text(lines[-1]):
            return lines[-1]
    if len(lines) == 1:
        return lines[0]
    return lines[-1]


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
    if len(shorter) >= len(longer):
        return False
    return longer[-len(shorter) :] == shorter


def choose_inbound_text(panel: dict[str, Any], fallback_preview: str = "") -> str:
    preview = str(fallback_preview or "").strip()
    latest_outbound = str(panel.get("latestOutbound") or "").strip()
    preview_is_outbound = _text_matches_outbound(preview, latest_outbound)
    inbound_items = _meaningful_inbound_items(panel)
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
                return "\n".join(str(item.get("text", "")).strip() for item in recent_rows if item.get("text"))
        tail_rows = _tail_cluster(inbound_items)
        if tail_rows:
            return "\n".join(
                str(item.get("text", "")).strip()
                for item in tail_rows[-3:]
                if item.get("text")
            )
    latest_inbound = str(panel.get("latestInbound") or "").strip()
    if is_history_marker(latest_inbound):
        latest_inbound = ""
    latest_inbound = _clean_multiline_latest_inbound(latest_inbound, preview) or latest_inbound
    if is_history_marker(preview):
        preview = ""
        preview_is_outbound = False
    if preview_is_outbound:
        preview = ""
    if wechat_ui.is_nontext_message(preview):
        return preview
    if wechat_ui.has_meaningful_text(latest_inbound):
        return latest_inbound
    if wechat_ui.has_meaningful_text(preview):
        return preview
    return latest_inbound or preview


def candidate_contact_name(candidate: dict[str, Any]) -> str:
    return str(candidate.get("matchedContact") or candidate.get("name", "")).strip()


def choose_whitelist_candidates(probe_result: dict[str, Any], allowed_contacts: list[str]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for chat in probe_result.get("visibleChats", []):
        if not chat.get("unread"):
            continue
        matched_contact = next(
            (allowed for allowed in allowed_contacts if wechat_ui.names_match(chat.get("name", ""), allowed)),
            "",
        )
        if matched_contact:
            candidate = copy.deepcopy(chat)
            candidate["matchedContact"] = matched_contact
            candidates.append(candidate)
    return candidates


def choose_non_whitelist_unread(probe_result: dict[str, Any], allowed_contacts: list[str]) -> list[dict[str, Any]]:
    unread: list[dict[str, Any]] = []
    for chat in probe_result.get("visibleChats", []):
        if not chat.get("unread"):
            continue
        if any(wechat_ui.names_match(chat.get("name", ""), allowed) for allowed in allowed_contacts):
            continue
        unread.append(copy.deepcopy(chat))
    return unread


def choose_active_whitelist_candidate(probe_result: dict[str, Any], allowed_contacts: list[str]) -> dict[str, Any]:
    active_chat = str(probe_result.get("activeChat") or "").strip()
    if not active_chat:
        return {}
    matched_contact = next(
        (allowed for allowed in allowed_contacts if wechat_ui.names_match(active_chat, allowed)),
        "",
    )
    if not matched_contact:
        return {}
    panel = probe_result.get("chatPanel", {}) or {}
    if latest_message_is_outbound(panel):
        return {}
    visible = find_visible_chat(probe_result, matched_contact)
    inbound_text = choose_inbound_text(panel, str(visible.get("preview") or ""))
    if not inbound_text:
        return {}
    return {
        "name": active_chat,
        "matchedContact": matched_contact,
        "preview": str(visible.get("preview") or ""),
        "time": str(visible.get("time") or ""),
        "source": "active_chat_fallback",
    }


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
    for chat in probe_result.get("visibleChats", []):
        matched_contact = next(
            (allowed for allowed in allowed_contacts if wechat_ui.names_match(chat.get("name", ""), allowed)),
            "",
        )
        if not matched_contact:
            continue
        if active_chat and active_latest_outbound and wechat_ui.names_match(matched_contact, active_chat):
            continue
        preview = str(chat.get("preview") or "").strip()
        if not preview or is_history_marker(preview) or not wechat_ui.has_meaningful_text(preview):
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


def find_visible_chat(probe_result: dict[str, Any], contact: str) -> dict[str, Any]:
    for chat in probe_result.get("visibleChats", []):
        if wechat_ui.names_match(str(chat.get("name", "")), contact):
            return chat
    return {}


def get_pending_queue(state: dict[str, Any]) -> list[dict[str, Any]]:
    queue = state.get("pending_queue")
    if isinstance(queue, list):
        return queue
    pending = state.get("pending")
    queue = [pending] if pending else []
    state["pending_queue"] = queue
    state["pending"] = queue[0] if queue else None
    return queue


def sync_pending_state(state: dict[str, Any], queue: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    current = queue if queue is not None else get_pending_queue(state)
    state["pending_queue"] = current
    state["pending"] = current[0] if current else None
    return current


def queued_contacts(queue: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("contact", "")).strip() for item in queue if item.get("contact")]


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


def latest_message_is_outbound(panel: dict[str, Any], *, max_top: float = 0.90, epsilon: float = 0.01) -> bool:
    inbound_top = latest_meaningful_inbound_top(panel)
    outbound_item = latest_committed_outbound_item(panel, max_top=max_top)
    if inbound_top is None or not outbound_item:
        return False
    outbound_text = str(outbound_item.get("text") or "").strip()
    if not wechat_ui.has_meaningful_text(outbound_text) or is_history_marker(outbound_text):
        return False
    outbound_top = float(outbound_item.get("top", 0.0) or 0.0)
    return outbound_top > inbound_top + epsilon


def find_queue_index_for_contact(queue: list[dict[str, Any]], contact: str) -> int:
    for index, item in enumerate(queue):
        if wechat_ui.names_match(str(item.get("contact", "")).strip(), contact):
            return index
    return -1


def remove_pending_by_fingerprint(queue: list[dict[str, Any]], pending: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in queue
        if item.get("inbound_fingerprint") != pending.get("inbound_fingerprint")
    ]


def _pending_anchor_ts(item: dict[str, Any]) -> float:
    due_at = float(item.get("due_at", 0.0) or 0.0)
    created_at = float(item.get("created_at", 0.0) or 0.0)
    if due_at > 0:
        return due_at
    return created_at


def prune_stale_pending(
    queue: list[dict[str, Any]],
    *,
    now: float,
    ttl_seconds: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if ttl_seconds <= 0:
        return list(queue), []
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for item in queue:
        anchor = _pending_anchor_ts(item)
        if anchor <= 0:
            kept.append(item)
            continue
        age = now - anchor
        if age > ttl_seconds:
            removed.append(item)
            continue
        kept.append(item)
    return kept, removed


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

    def _menu_unread_signal(self) -> str:
        if hasattr(self.vision, "unread_signal"):
            try:
                return str(self.vision.unread_signal() or "")
            except Exception:
                pass
        return "1" if bool(self.vision.check_unread_dot()) else ""

    def tick(self) -> dict[str, Any]:
        config = self.load_config()
        state = self.load_state()
        state["last_run_at"] = utc_now_iso()
        now = float(self.now())
        result: dict[str, Any]

        try:
            if not config.get("enabled"):
                state["last_error"] = ""
                result = {"status": "disabled"}
                return result

            idle_seconds = float(self.idle.get_idle_time_seconds())
            idle_threshold = float(config.get("idle_threshold_seconds", 30))
            idle_probe_armed = bool(state.get("idle_probe_armed", True))
            if idle_seconds < idle_threshold:
                # While user is active, don't sample menubar digits; clear transient menu signal
                # to avoid stale "has digit" state triggering a late false open.
                state["idle_probe_armed"] = True
                idle_probe_armed = True
                state["last_menu_unread"] = False
                state["last_menu_signal"] = ""
                state["last_claim_menu_signal"] = ""
                state["pending_menu_clear_streak"] = 0

            menu_checked_now = False
            should_check_menu = False
            if idle_seconds >= idle_threshold:
                check_interval_due = now - float(state.get("last_menu_check_at", 0.0) or 0.0) >= float(
                    config.get("menubar_check_interval_seconds", 15)
                )
                should_check_menu = check_interval_due or idle_probe_armed
            if should_check_menu:
                menu_signal = self._menu_unread_signal()
                has_unread = bool(menu_signal)
                state["last_menu_unread"] = has_unread
                state["last_menu_signal"] = menu_signal
                state["last_menu_check_at"] = now
                menu_checked_now = True
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
                self.append_event("menu_bar_checked", unread=has_unread, signal=menu_signal)

            cleanup_interval = float(config.get("capture_cleanup_interval_seconds", 3600))
            if now - float(state.get("last_capture_cleanup_at", 0.0) or 0.0) >= cleanup_interval:
                cleanup = delete_capture_snapshots_older_than(
                    older_than_seconds=float(config.get("capture_retention_days", 1)) * 24 * 60 * 60,
                    now=now,
                )
                state["last_capture_cleanup_at"] = now
                if int(cleanup.get("deleted_count", 0) or 0) > 0:
                    self.append_event(
                        "capture_cleanup",
                        deleted_count=int(cleanup.get("deleted_count", 0) or 0),
                        deleted_bytes=int(cleanup.get("deleted_bytes", 0) or 0),
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
            current_menu_signal = str(state.get("last_menu_signal") or "")
            actionable_menu_signal = is_actionable_menu_signal(current_menu_signal)
            should_sweep = (
                idle_seconds >= idle_threshold
                and actionable_menu_signal
                and menu_checked_now
            )
            claim_result: dict[str, Any] | None = None
            if should_sweep:
                state["idle_probe_armed"] = False
                state["last_claim_menu_signal"] = current_menu_signal
                claim_result = self._handle_claim(
                    config,
                    state,
                    idle_seconds,
                    now,
                )
                queue = sync_pending_state(state)

            if claim_result and not had_queue and claim_result.get("status") in {"draft_saved", "drafts_saved"}:
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
        return OllamaClient(
            url=str(config.get("ollama_url")),
            model=str(config.get("ollama_model")),
            max_reply_chars=int(config.get("max_reply_chars", 90)),
            style_instructions=str(config.get("reply_style_instructions", "")),
        )

    def _handle_claim(
        self,
        config: dict[str, Any],
        state: dict[str, Any],
        idle_seconds: float,
        now: float,
    ) -> dict[str, Any]:
        state["last_roster_sweep_at"] = now
        self.append_event(
            "wechat_window_action",
            action="open",
            reason="claim_scan",
            queue_contacts=queued_contacts(sync_pending_state(state)),
        )
        self.ui.activate_wechat()
        try:
            allowed_contacts = list(config.get("allowed_contacts", []))
            queue = sync_pending_state(state)
            probe_result = self.ui.probe()
            candidates = choose_whitelist_candidates(probe_result, allowed_contacts)
            self.append_event(
                "claim_candidates",
                contacts=[chat.get("name", "") for chat in candidates],
                queue_contacts=queued_contacts(queue),
            )
            if not candidates:
                non_whitelist_unread = choose_non_whitelist_unread(probe_result, allowed_contacts)
                non_whitelist_cleared = False
                if non_whitelist_unread:
                    cleared_contacts: list[str] = []
                    seen_names: set[str] = set()
                    for chat in non_whitelist_unread:
                        contact = str(chat.get("name", "")).strip()
                        key = normalize_text(contact)
                        if not contact or key in seen_names:
                            continue
                        seen_names.add(key)
                        self.ui.probe(select_chat=contact, sleep_after_click=0.25)
                        cleared_contacts.append(contact)
                    if cleared_contacts:
                        non_whitelist_cleared = True
                        self.append_event(
                            "non_whitelist_unread_cleared",
                            contacts=cleared_contacts,
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
            queue_fingerprints = {str(item.get("inbound_fingerprint", "")) for item in queue}
            llm = self._build_llm(config)
            def process_candidates(snapshot_candidates: list[dict[str, Any]]) -> None:
                nonlocal queue
                nonlocal queue_fingerprints
                for candidate in snapshot_candidates:
                    contact = candidate_contact_name(candidate)
                    existing_index = find_queue_index_for_contact(queue, contact)
                    selected = self.ui.probe(select_chat=contact)
                    if selected.get("status") != "ok" or not selected.get("selectionConfirmed"):
                        self.append_event("claim_skipped", reason="selection_not_confirmed", contact=contact)
                        continue

                    panel = selected.get("chatPanel", {}) or {}
                    preview_text = str(candidate.get("preview") or "")
                    outbound_snapshot = latest_committed_outbound(panel)
                    if _text_matches_outbound(preview_text, outbound_snapshot):
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
                    if latest_message_is_outbound(panel):
                        if existing_index >= 0:
                            pending_existing = queue[existing_index]
                            self._cancel_pending(
                                state,
                                "manual_reply_detected_claim",
                                pending_existing,
                            )
                            queue = sync_pending_state(state)
                            queue_fingerprints = {str(item.get("inbound_fingerprint", "")) for item in queue}
                        self.append_event("claim_skipped", reason="latest_message_outbound", contact=contact)
                        continue
                    inbound_text = choose_inbound_text(panel, str(candidate.get("preview") or ""))
                    if not inbound_text:
                        self.append_event("claim_skipped", reason="empty_inbound", contact=contact)
                        continue

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

                    draft_text = llm.generate_reply(contact, inbound_text)
                    pending = {
                        "contact": contact,
                        "inbound_text": inbound_text,
                        "message_time": message_time,
                        "inbound_fingerprint": inbound_fingerprint,
                        "draft_text": draft_text,
                        "created_at": now,
                        "due_at": now + float(config.get("send_delay_seconds", 300)),
                        "outbound_snapshot": outbound_snapshot,
                        "active_chat_title": selected.get("activeChat", ""),
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
                        queue_length=len(queue),
                        queue_contacts=queued_contacts(queue),
                    )

            process_candidates(candidates)
            initial_contacts = {candidate_contact_name(candidate) for candidate in candidates if candidate_contact_name(candidate)}
            follow_up_probe = self.ui.probe()
            follow_up_candidates = [
                candidate
                for candidate in choose_whitelist_candidates(follow_up_probe, allowed_contacts)
                if candidate_contact_name(candidate) not in initial_contacts
            ]
            if follow_up_candidates:
                self.append_event(
                    "claim_follow_up_candidates",
                    contacts=[chat.get("name", "") for chat in follow_up_candidates],
                    queue_contacts=queued_contacts(queue),
                )
                process_candidates(follow_up_candidates)

            changed = added + refreshed
            if not changed:
                return {"status": "no_candidate"}
            if len(changed) == 1:
                return {"status": "draft_saved", "contact": changed[0], "queue_length": len(queue)}
            return {"status": "drafts_saved", "contacts": changed, "queue_length": len(queue)}
        finally:
            self.append_event("wechat_window_action", action="hide", reason="claim_scan")
            self.ui.hide_wechat()

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
    ) -> dict[str, Any]:
        existing = queue[pending_index]
        contact = str(existing.get("contact", "")).strip()
        client = llm or self._build_llm(config)
        draft_text = client.generate_reply(contact, inbound_text)
        updated = copy.deepcopy(existing)
        updated.update(
            {
                "inbound_text": inbound_text,
                "message_time": message_time,
                "inbound_fingerprint": fingerprint(contact, inbound_text, message_time),
                "draft_text": draft_text,
                "created_at": now,
                "due_at": now + float(config.get("send_delay_seconds", 300)),
                "outbound_snapshot": outbound_snapshot,
                "active_chat_title": selected.get("activeChat", ""),
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
            draft_text=draft_text,
            due_at=updated["due_at"],
            idle_seconds=round(idle_seconds, 2),
            queue_length=len(queue),
            queue_contacts=queued_contacts(queue),
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
        if idle_seconds < float(config.get("idle_threshold_seconds", 30)):
            return {
                "status": "pending_wait_user_active",
                "idle_seconds": round(idle_seconds, 2),
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

        contact = str(pending.get("contact", "")).strip()
        self.append_event("wechat_window_action", action="open", reason="pending_send_due", contact=contact)
        self.ui.activate_wechat()
        try:
            selected = self.ui.probe(select_chat=contact)
            if selected.get("status") != "ok" or not selected.get("selectionConfirmed"):
                return self._cancel_pending(state, "selection_not_confirmed", pending)

            panel = selected.get("chatPanel", {}) or {}
            visible_chat = find_visible_chat(selected, contact)
            current_preview = str(visible_chat.get("preview") or "")
            current_time = str(visible_chat.get("time") or "")
            current_inbound = choose_inbound_text(panel, current_preview)
            current_outbound_item = latest_committed_outbound_item(panel)
            current_outbound = latest_committed_outbound(panel)
            if current_outbound and _text_matches_outbound(current_preview, current_outbound):
                return self._cancel_pending(
                    state,
                    "manual_reply_detected_preview",
                    pending,
                    current_outbound=current_outbound,
                )
            current_outbound_top = (
                float(current_outbound_item.get("top", 0.0) or 0.0) if current_outbound_item else None
            )
            latest_inbound_top = latest_meaningful_inbound_top(panel)
            latest_bubble_outbound = latest_message_is_outbound(panel)
            draft_text = str(pending.get("draft_text", ""))
            if current_outbound and (
                normalize_text(current_inbound) == normalize_text(current_outbound)
                or normalize_text(current_inbound) == normalize_text(draft_text)
            ):
                current_inbound = ""
            if normalize_text(current_outbound) == normalize_text(draft_text) and current_outbound:
                if int(pending.get("send_attempts", 0) or 0) > 0:
                    remaining = remove_pending_by_fingerprint(queue, pending)
                    sync_pending_state(state, remaining)
                    self.append_event(
                        "auto_sent",
                        contact=contact,
                        draft_text=draft_text,
                        remaining_queue=len(remaining),
                        queue_contacts=queued_contacts(remaining),
                        confirmation="late",
                    )
                    return {"status": "sent", "contact": contact, "queue_length": len(remaining)}
                return self._cancel_pending(state, "reply_already_present", pending)

            snapshot_outbound = normalize_text(str(pending.get("outbound_snapshot", "")))
            outbound_after_latest_inbound = bool(
                current_outbound
                and current_outbound_top is not None
                and latest_inbound_top is not None
                and current_outbound_top > latest_inbound_top + 0.01
            )
            if latest_bubble_outbound:
                return self._cancel_pending(state, "manual_reply_detected", pending, current_outbound=current_outbound)
            if (
                current_outbound
                and normalize_text(current_outbound) != snapshot_outbound
                and (snapshot_outbound or outbound_after_latest_inbound or latest_inbound_top is None)
            ):
                return self._cancel_pending(state, "manual_reply_detected", pending, current_outbound=current_outbound)

            if not current_inbound:
                return self._cancel_pending(state, "empty_inbound_recheck", pending)

            if inbound_variant_equivalent(
                str(pending.get("inbound_text", "")),
                current_inbound,
                pending_time=str(pending.get("message_time", "")),
                current_time=current_time,
            ):
                current_inbound = str(pending.get("inbound_text", "")) or current_inbound
                current_time = str(pending.get("message_time", "")) or current_time

            if fingerprint(contact, current_inbound, current_time) != pending.get("inbound_fingerprint"):
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

            self.ui.focus_input_box(selected)
            self.ui.paste_text(draft_text)
            self.ui.send_message()
            confirmed = self.ui.probe(select_chat=contact, sleep_after_click=0.4)
            confirmed_panel = confirmed.get("chatPanel", {}) or {}
            confirmed_outbound = latest_committed_outbound(confirmed_panel)
            if normalize_text(confirmed_outbound) == normalize_text(draft_text) and confirmed_outbound:
                remaining = remove_pending_by_fingerprint(queue, pending)
                sync_pending_state(state, remaining)
                self.append_event(
                    "auto_sent",
                    contact=contact,
                    draft_text=draft_text,
                    remaining_queue=len(remaining),
                    queue_contacts=queued_contacts(remaining),
                    confirmation="immediate",
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
            self.append_event("wechat_window_action", action="hide", reason="pending_send_due", contact=contact)
            self.ui.hide_wechat()


class _VisionSensor:
    def unread_signal(self) -> str:
        return str(unread_signal() or "")

    def check_unread_dot(self) -> bool:
        return bool(check_unread_dot())


class _IdleSensor:
    def get_idle_time_seconds(self) -> float:
        return float(get_idle_time_seconds())
