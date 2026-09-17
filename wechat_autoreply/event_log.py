from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
import stat
import time
from typing import Any

from .paths import EVENTS_LOCK_PATH, EVENTS_PATH, ensure_runtime_dirs


FALLBACK_EVENTS_PATH = Path.home() / ".openclaw" / "logs" / "wechat-autoreply-events-fallback.jsonl"
RETRYABLE_ERRNOS = {errno.EAGAIN, errno.EDEADLK}


def _is_dataless(path: Path) -> bool:
    try:
        flags = int(getattr(path.stat(), "st_flags", 0) or 0)
    except OSError:
        return False
    return bool(flags & int(getattr(stat, "SF_DATALESS", 0) or 0))


def _archive_unreadable_events_file(path: Path, *, force: bool = False) -> Path | None:
    if not path.exists() or (not force and not _is_dataless(path)):
        return None
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    archive_path = path.with_name(f"{path.stem}.dataless-{stamp}{path.suffix}")
    counter = 1
    while archive_path.exists():
        archive_path = path.with_name(f"{path.stem}.dataless-{stamp}-{counter}{path.suffix}")
        counter += 1
    path.replace(archive_path)
    return archive_path


def ensure_events_file_local(events_path: Path = EVENTS_PATH, *, force: bool = False) -> Path | None:
    if not events_path.exists() or (not force and not _is_dataless(events_path)):
        return None
    with event_file_lock(events_path):
        return _archive_unreadable_events_file(events_path, force=force)


def read_event_lines(events_path: Path = EVENTS_PATH) -> list[str]:
    if not events_path.exists():
        return []
    ensure_events_file_local(events_path)
    if not events_path.exists():
        return []
    try:
        return events_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        if exc.errno not in RETRYABLE_ERRNOS:
            raise
        ensure_events_file_local(events_path, force=True)
        return []


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
    ensure_events_file_local(EVENTS_PATH)
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
        if last_error.errno in RETRYABLE_ERRNOS:
            ensure_events_file_local(EVENTS_PATH, force=True)
            with event_file_lock(EVENTS_PATH):
                _append_line(EVENTS_PATH, line)
            return
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
