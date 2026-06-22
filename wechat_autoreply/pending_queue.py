from __future__ import annotations

from typing import Any


def get_pending_queue(state: dict[str, Any]) -> list[dict[str, Any]]:
    queue = state.get("pending_queue")
    if isinstance(queue, list):
        return queue
    pending = state.get("pending")
    queue = [pending] if pending else []
    state["pending_queue"] = queue
    state["pending"] = queue[0] if queue else None
    return queue


def sync_pending_state(
    state: dict[str, Any],
    queue: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    current = queue if queue is not None else get_pending_queue(state)
    state["pending_queue"] = current
    state["pending"] = current[0] if current else None
    return current


def queued_contacts(queue: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("contact", "")).strip() for item in queue if item.get("contact")]


def find_queue_index_for_contact(queue: list[dict[str, Any]], contact: str) -> int:
    from . import wechat_ui

    for index, item in enumerate(queue):
        if wechat_ui.names_match(str(item.get("contact", "")).strip(), contact):
            return index
    return -1


def remove_pending_by_fingerprint(
    queue: list[dict[str, Any]],
    pending: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        item
        for item in queue
        if item.get("inbound_fingerprint") != pending.get("inbound_fingerprint")
    ]


def _pending_anchor_ts(item: dict[str, Any]) -> float:
    due_at = float(item.get("due_at", 0.0) or 0.0)
    created_at = float(item.get("created_at", 0.0) or 0.0)
    return due_at if due_at > 0 else created_at


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
        if anchor <= 0 or now - anchor <= ttl_seconds:
            kept.append(item)
        else:
            removed.append(item)
    return kept, removed
