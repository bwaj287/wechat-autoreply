from __future__ import annotations

import json
import math
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .paths import CONTACT_MEMORY_PATH, CONTACT_MEMORY_SEED_PATH, ensure_runtime_dirs

_SHORT_NOISE_RE = re.compile(r"^[~～`'\"!！?？.,，。…·•\\-_/|]{1,6}$")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _clean_line(text: str, max_chars: int = 96) -> str:
    value = " ".join(str(text or "").strip().split())
    if not value:
        return ""
    return value[:max_chars].strip()


def _is_meaningful_memory_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if _SHORT_NOISE_RE.fullmatch(value):
        return False
    if len(value) == 1 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", value):
        return False
    return True


def _parse_dt(value: str) -> datetime | None:
    try:
        raw = str(value or "").strip()
        if not raw:
            return None
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def default_contact_memory_store() -> dict[str, Any]:
    return {
        "version": 2,
        "contacts": {},
    }


def _default_contact_entry() -> dict[str, Any]:
    return {
        "profile": "",
        "profile_locked": False,
        "recent_summary": "",
        "recent_events": [],
        "updated_at": "",
        "profile_updated_at": "",
    }


def _load_seed_contacts() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(CONTACT_MEMORY_SEED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    contacts = raw.get("contacts") if isinstance(raw, dict) else {}
    seeded: dict[str, dict[str, Any]] = {}
    for key, value in (contacts if isinstance(contacts, dict) else {}).items():
        entry = _default_contact_entry()
        if isinstance(value, dict):
            entry.update(value)
        seeded[str(key)] = entry
    return seeded


def load_contact_memory_store() -> dict[str, Any]:
    ensure_runtime_dirs()
    if not CONTACT_MEMORY_PATH.exists():
        store = default_contact_memory_store()
        _atomic_write(CONTACT_MEMORY_PATH, store)
        return store
    try:
        raw = json.loads(CONTACT_MEMORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    merged = default_contact_memory_store()
    merged.update(raw if isinstance(raw, dict) else {})
    merged["version"] = default_contact_memory_store()["version"]
    contacts = merged.get("contacts")
    normalized_contacts: dict[str, Any] = {}
    for key, value in (contacts if isinstance(contacts, dict) else {}).items():
        entry = _default_contact_entry()
        if isinstance(value, dict):
            entry.update(value)
        normalized_contacts[str(key)] = entry
    for key, value in _load_seed_contacts().items():
        if key not in normalized_contacts:
            normalized_contacts[key] = value
            continue
        existing = dict(normalized_contacts[key] or {})
        if not str(existing.get("profile") or "").strip() and str(value.get("profile") or "").strip():
            existing["profile"] = str(value.get("profile") or "").strip()
        if "profile_locked" not in existing:
            existing["profile_locked"] = bool(value.get("profile_locked"))
        normalized_contacts[key] = existing
    merged["contacts"] = normalized_contacts
    if merged != raw:
        _atomic_write(CONTACT_MEMORY_PATH, merged)
    return merged


def save_contact_memory_store(store: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    merged = default_contact_memory_store()
    merged.update(store if isinstance(store, dict) else {})
    merged["version"] = default_contact_memory_store()["version"]
    contacts = merged.get("contacts")
    normalized_contacts: dict[str, Any] = {}
    for key, value in (contacts if isinstance(contacts, dict) else {}).items():
        entry = _default_contact_entry()
        if isinstance(value, dict):
            entry.update(value)
        normalized_contacts[str(key)] = entry
    merged["contacts"] = normalized_contacts
    _atomic_write(CONTACT_MEMORY_PATH, merged)


def _resolve_contact_key(store: dict[str, Any], contact: str) -> str:
    contacts = dict(store.get("contacts") or {})
    target = _normalize_text(contact)
    for key in contacts:
        if _normalize_text(key) == target:
            return key
    return str(contact or "").strip()


def get_contact_memory(contact: str) -> dict[str, Any]:
    store = load_contact_memory_store()
    key = _resolve_contact_key(store, contact)
    entry = _default_contact_entry()
    entry.update(dict((store.get("contacts") or {}).get(key) or {}))
    return {
        "contact": key,
        "profile": str(entry.get("profile") or "").strip(),
        "profile_locked": bool(entry.get("profile_locked")),
        "recent_summary": str(entry.get("recent_summary") or "").strip(),
        "recent_events": list(entry.get("recent_events") or []),
        "updated_at": str(entry.get("updated_at") or "").strip(),
        "profile_updated_at": str(entry.get("profile_updated_at") or "").strip(),
    }


def set_contact_profile(contact: str, profile: str, *, locked: bool | None = None) -> dict[str, Any]:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    store = load_contact_memory_store()
    key = _resolve_contact_key(store, contact)
    contacts = dict(store.get("contacts") or {})
    entry = _default_contact_entry()
    entry.update(dict(contacts.get(key) or {}))
    entry["profile"] = _clean_line(profile, max_chars=320)
    if locked is not None:
        entry["profile_locked"] = bool(locked)
    entry["updated_at"] = timestamp
    entry["profile_updated_at"] = timestamp
    contacts[key] = entry
    store["contacts"] = contacts
    save_contact_memory_store(store)
    return get_contact_memory(key)


def clear_contact_recent_memory(contact: str) -> dict[str, Any]:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    store = load_contact_memory_store()
    key = _resolve_contact_key(store, contact)
    contacts = dict(store.get("contacts") or {})
    entry = _default_contact_entry()
    entry.update(dict(contacts.get(key) or {}))
    entry["recent_summary"] = ""
    entry["recent_events"] = []
    entry["updated_at"] = timestamp
    contacts[key] = entry
    store["contacts"] = contacts
    save_contact_memory_store(store)
    return get_contact_memory(key)


def set_contact_profile_lock(contact: str, locked: bool) -> dict[str, Any]:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    store = load_contact_memory_store()
    key = _resolve_contact_key(store, contact)
    contacts = dict(store.get("contacts") or {})
    entry = _default_contact_entry()
    entry.update(dict(contacts.get(key) or {}))
    entry["profile_locked"] = bool(locked)
    entry["updated_at"] = timestamp
    contacts[key] = entry
    store["contacts"] = contacts
    save_contact_memory_store(store)
    return get_contact_memory(key)


def _event_weight(event: dict[str, Any], now: datetime) -> float:
    event_time = _parse_dt(str(event.get("ts") or "")) or now
    age = max(0.0, (now - event_time).total_seconds() / 3600.0)
    return math.exp(-age / 72.0)


def _summarize_recent_events(events: list[dict[str, Any]], now: datetime) -> str:
    if not events:
        return ""
    scored: list[tuple[float, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for event in reversed(events):
        role = "self" if str(event.get("role") or "").strip().lower() == "self" else "contact"
        text = _clean_line(str(event.get("text") or ""), max_chars=84)
        if not _is_meaningful_memory_text(text):
            continue
        token = (role, _normalize_text(text))
        if token in seen:
            continue
        seen.add(token)
        scored.append((_event_weight(event, now), {"role": role, "text": text}))
    if not scored:
        return ""
    scored.sort(key=lambda item: item[0], reverse=True)
    contact_lines = [item["text"] for _, item in scored if item["role"] == "contact"][:3]
    self_lines = [item["text"] for _, item in scored if item["role"] == "self"][:2]
    parts: list[str] = []
    if contact_lines:
        if len(contact_lines) == 1:
            parts.append(f"They recently mentioned {contact_lines[0]}")
        else:
            parts.append("They recently mentioned " + " / ".join(contact_lines))
    if self_lines:
        if len(self_lines) == 1:
            parts.append(f"You recently replied {self_lines[0]}")
        else:
            parts.append("You recently replied " + " / ".join(self_lines))
    summary = "; ".join(parts).strip()
    return summary[:320].strip()


def remember_contact_memory(
    contact: str,
    *,
    context_messages: list[dict[str, str]] | None = None,
    inbound_text: str = "",
    outbound_text: str = "",
    now: datetime | None = None,
    max_events: int = 18,
    retention_days: int = 14,
) -> dict[str, Any]:
    timestamp = now or datetime.now().astimezone()
    store = load_contact_memory_store()
    key = _resolve_contact_key(store, contact)
    contacts = dict(store.get("contacts") or {})
    entry = _default_contact_entry()
    entry.update(dict(contacts.get(key) or {}))
    profile = str(entry.get("profile") or "").strip()
    profile_locked = bool(entry.get("profile_locked"))
    recent_events = list(entry.get("recent_events") or [])

    cutoff = timestamp - timedelta(days=max(1, int(retention_days)))
    filtered_events: list[dict[str, Any]] = []
    for item in recent_events:
        if not isinstance(item, dict):
            continue
        event_time = _parse_dt(str(item.get("ts") or ""))
        if event_time and event_time < cutoff:
            continue
        text = _clean_line(str(item.get("text") or ""))
        if not _is_meaningful_memory_text(text):
            continue
        role = "self" if str(item.get("role") or "").strip().lower() == "self" else "contact"
        filtered_events.append({"role": role, "text": text, "ts": str(item.get("ts") or "")})

    for item in list(context_messages or [])[-6:]:
        if not isinstance(item, dict):
            continue
        role = "self" if str(item.get("role") or "").strip().lower() == "self" else "contact"
        text = _clean_line(str(item.get("text") or ""))
        if not _is_meaningful_memory_text(text):
            continue
        filtered_events.append({"role": role, "text": text, "ts": timestamp.isoformat(timespec="seconds")})

    latest_inbound = _clean_line(inbound_text)
    if _is_meaningful_memory_text(latest_inbound):
        filtered_events.append({"role": "contact", "text": latest_inbound, "ts": timestamp.isoformat(timespec="seconds")})

    latest_outbound = _clean_line(outbound_text)
    if _is_meaningful_memory_text(latest_outbound):
        filtered_events.append({"role": "self", "text": latest_outbound, "ts": timestamp.isoformat(timespec="seconds")})

    deduped: list[dict[str, Any]] = []
    for item in filtered_events:
        text = str(item.get("text") or "").strip()
        role = "self" if str(item.get("role") or "").strip().lower() == "self" else "contact"
        if deduped and role == deduped[-1].get("role") and _normalize_text(text) == _normalize_text(
            str(deduped[-1].get("text") or "")
        ):
            deduped[-1]["ts"] = str(item.get("ts") or deduped[-1].get("ts") or "")
            continue
        deduped.append({"role": role, "text": text, "ts": str(item.get("ts") or "")})

    deduped = deduped[-max(6, int(max_events)) :]
    recent_summary = _summarize_recent_events(deduped, timestamp)
    contacts[key] = {
        "profile": profile,
        "profile_locked": profile_locked,
        "recent_summary": recent_summary,
        "recent_events": deduped,
        "updated_at": timestamp.isoformat(timespec="seconds"),
        "profile_updated_at": str(entry.get("profile_updated_at") or ""),
    }
    store["contacts"] = contacts
    save_contact_memory_store(store)
    return {
        "contact": key,
        "profile": profile,
        "profile_locked": profile_locked,
        "recent_summary": recent_summary,
        "recent_events": deduped,
        "updated_at": timestamp.isoformat(timespec="seconds"),
        "profile_updated_at": str(entry.get("profile_updated_at") or ""),
    }
