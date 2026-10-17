import json
from datetime import datetime, timezone
from typing import Any

from .json_output import write_json_atomically as _atomic_write
from .paths import RUNTIME_DIR, STATE_PATH, ensure_runtime_dirs
from .pending_queue import sync_pending_state

LEGACY_STATE_PATH = RUNTIME_DIR / "state.json"


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
        "claim_retry_candidate": None,
        "wechat_login_required": False,
        "wechat_window_unavailable": False,
        "last_wechat_login_required_event_at": 0.0,
        "last_capture_cleanup_at": 0.0,
        "last_roster_sweep_at": 0.0,
        "last_seen_inbound": {},
        "recent_auto_outbounds": {},
        "badge_streaks": {},
        "pending_queue": [],
        "pending": None,
    }


def _migrate_legacy_state_if_needed() -> None:
    if STATE_PATH.exists() or not LEGACY_STATE_PATH.exists() or LEGACY_STATE_PATH == STATE_PATH:
        return
    try:
        legacy = json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(legacy, dict):
        _atomic_write(STATE_PATH, legacy)


def load_state() -> dict[str, Any]:
    ensure_runtime_dirs()
    _migrate_legacy_state_if_needed()
    if not STATE_PATH.exists():
        state = default_state()
        _atomic_write(STATE_PATH, state)
        return state
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    merged = default_state()
    merged.update(state)
    merged.pop("menu_claim_suppressed", None)
    if merged.get("pending") and not list(merged.get("pending_queue") or []):
        merged["pending_queue"] = [merged["pending"]]
    sync_pending_state(merged, list(merged.get("pending_queue") or []))
    if merged != state:
        _atomic_write(STATE_PATH, merged)
    return merged


def save_state(state: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    merged = default_state()
    merged.update(state)
    merged.pop("menu_claim_suppressed", None)
    if merged.get("pending") and not list(merged.get("pending_queue") or []):
        merged["pending_queue"] = [merged["pending"]]
    sync_pending_state(merged, list(merged.get("pending_queue") or []))
    _atomic_write(STATE_PATH, merged)
