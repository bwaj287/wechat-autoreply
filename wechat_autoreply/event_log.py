from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any

from .paths import EVENTS_LOCK_PATH, EVENTS_PATH, ensure_runtime_dirs


FALLBACK_EVENTS_PATH = Path.home() / ".openclaw" / "logs" / "wechat-autoreply-events-fallback.jsonl"
RETRYABLE_ERRNOS = {errno.EAGAIN, errno.EDEADLK}


@contextmanager
def event_file_lock(events_path: Path = EVENTS_PATH):
    ensure_runtime_dirs()
    lock_path = EVENTS_LOCK_PATH if events_path == EVENTS_PATH else events_path.with_name(f".{events_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def _append_primary_with_retry(line: str) -> None:
    delays = (0.0, 0.03, 0.12)
    last_error: OSError | None = None
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            with event_file_lock(EVENTS_PATH):
                _append_line(EVENTS_PATH, line)
            return
        except OSError as exc:
            last_error = exc
            if exc.errno not in RETRYABLE_ERRNOS:
                raise
    if last_error is not None:
        raise last_error


def _append_fallback(event: dict[str, Any], primary_error: Exception) -> None:
    fallback = dict(event)
    fallback["event_log_fallback"] = True
    fallback["primary_error"] = str(primary_error)
    try:
        _append_line(FALLBACK_EVENTS_PATH, json.dumps(fallback, ensure_ascii=False) + "\n")
    except Exception:
        # Logging must never take down the autoreply state machine.
        return


def append_event(event_type: str, **payload: Any) -> None:
    ensure_runtime_dirs()
    event = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "type": event_type,
    }
    event.update(payload)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        _append_primary_with_retry(line)
    except Exception as exc:
        _append_fallback(event, exc)
