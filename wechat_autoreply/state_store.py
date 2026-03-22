import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import STATE_PATH, ensure_runtime_dirs


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "last_run_at": "",
        "last_error": "",
        "last_menu_check_at": 0.0,
        "last_menu_unread": False,
        "last_menu_signal": "",
        "last_claim_menu_signal": "",
        "pending_menu_clear_streak": 0,
        "last_capture_cleanup_at": 0.0,
        "last_roster_sweep_at": 0.0,
        "last_seen_inbound": {},
        "pending_queue": [],
        "pending": None,
    }


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


def load_state() -> dict[str, Any]:
    ensure_runtime_dirs()
    if not STATE_PATH.exists():
        state = default_state()
        _atomic_write(STATE_PATH, state)
        return state
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    merged = default_state()
    merged.update(state)
    merged.pop("menu_claim_suppressed", None)
    pending_queue = list(merged.get("pending_queue") or [])
    pending = merged.get("pending")
    if pending and not pending_queue:
        pending_queue = [pending]
    merged["pending_queue"] = pending_queue
    merged["pending"] = pending_queue[0] if pending_queue else None
    if merged != state:
        _atomic_write(STATE_PATH, merged)
    return merged


def save_state(state: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    merged = default_state()
    merged.update(state)
    merged.pop("menu_claim_suppressed", None)
    pending_queue = list(merged.get("pending_queue") or [])
    pending = merged.get("pending")
    if pending and not pending_queue:
        pending_queue = [pending]
    merged["pending_queue"] = pending_queue
    merged["pending"] = pending_queue[0] if pending_queue else None
    _atomic_write(STATE_PATH, merged)
