from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .paths import CAPTURE_DIR, DEBUG_DIR, EVENTS_PATH, LOG_DIR


DEFAULT_CAPTURE_PATTERNS = ("*.png",)


def _delete_old_files(
    *,
    directory: Path,
    older_than_seconds: float,
    now: float,
    patterns: Iterable[str] = ("*",),
) -> dict[str, Any]:
    deleted_count = 0
    deleted_bytes = 0
    deleted_paths: list[str] = []
    seen: set[Path] = set()

    for pattern in patterns:
        for path in sorted(directory.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            age_seconds = now - float(stat.st_mtime)
            if age_seconds < float(older_than_seconds):
                continue
            size = int(stat.st_size)
            path.unlink(missing_ok=True)
            deleted_count += 1
            deleted_bytes += size
            deleted_paths.append(str(path))

    return {
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "deleted_paths": deleted_paths,
    }


def _prune_events_file(
    *,
    older_than_seconds: float,
    now: float,
    events_path: Path = EVENTS_PATH,
) -> dict[str, Any]:
    if not events_path.exists():
        return {"deleted_count": 0, "deleted_bytes": 0, "deleted_lines": 0, "kept_lines": 0}

    cutoff = datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(seconds=float(older_than_seconds))
    kept_lines: list[str] = []
    deleted_lines = 0
    deleted_bytes = 0

    with events_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            keep = True
            try:
                payload = json.loads(line)
                ts_text = str(payload.get("ts") or "").strip()
                if ts_text:
                    event_ts = datetime.fromisoformat(ts_text)
                    if event_ts.tzinfo is None:
                        event_ts = event_ts.replace(tzinfo=timezone.utc)
                    event_ts = event_ts.astimezone(timezone.utc)
                    if event_ts < cutoff:
                        keep = False
            except Exception:
                keep = True
            if keep:
                kept_lines.append(line)
            else:
                deleted_lines += 1
                deleted_bytes += len(raw_line.encode("utf-8"))

    if deleted_lines <= 0:
        return {"deleted_count": 0, "deleted_bytes": 0, "deleted_lines": 0, "kept_lines": len(kept_lines)}

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=events_path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        for line in kept_lines:
            handle.write(line + "\n")
    temp_path.replace(events_path)

    return {
        "deleted_count": 1,
        "deleted_bytes": deleted_bytes,
        "deleted_lines": deleted_lines,
        "kept_lines": len(kept_lines),
        "deleted_paths": [str(events_path)],
    }


def cleanup_runtime_artifacts_older_than(
    *,
    older_than_seconds: float = 2 * 24 * 60 * 60,
    now: float | None = None,
) -> dict[str, Any]:
    current_ts = float(now) if now is not None else time.time()
    captures = _delete_old_files(
        directory=CAPTURE_DIR,
        older_than_seconds=older_than_seconds,
        now=current_ts,
        patterns=DEFAULT_CAPTURE_PATTERNS,
    )
    debug = _delete_old_files(directory=DEBUG_DIR, older_than_seconds=older_than_seconds, now=current_ts)
    logs = _delete_old_files(directory=LOG_DIR, older_than_seconds=older_than_seconds, now=current_ts)
    events = _prune_events_file(older_than_seconds=older_than_seconds, now=current_ts)

    deleted_count = (
        int(captures.get("deleted_count", 0) or 0)
        + int(debug.get("deleted_count", 0) or 0)
        + int(logs.get("deleted_count", 0) or 0)
        + int(events.get("deleted_count", 0) or 0)
    )
    deleted_bytes = (
        int(captures.get("deleted_bytes", 0) or 0)
        + int(debug.get("deleted_bytes", 0) or 0)
        + int(logs.get("deleted_bytes", 0) or 0)
        + int(events.get("deleted_bytes", 0) or 0)
    )
    deleted_paths = [
        *list(captures.get("deleted_paths", []) or []),
        *list(debug.get("deleted_paths", []) or []),
        *list(logs.get("deleted_paths", []) or []),
        *list(events.get("deleted_paths", []) or []),
    ]

    return {
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "deleted_paths": deleted_paths,
        "retention_seconds": float(older_than_seconds),
        "captures_deleted": int(captures.get("deleted_count", 0) or 0),
        "debug_deleted": int(debug.get("deleted_count", 0) or 0),
        "logs_deleted": int(logs.get("deleted_count", 0) or 0),
        "events_deleted": int(events.get("deleted_lines", 0) or 0),
        "events_file_rewritten": int(events.get("deleted_count", 0) or 0) > 0,
    }


def delete_capture_snapshots_older_than(
    *,
    older_than_seconds: float = 2 * 24 * 60 * 60,
    capture_dir: Path = CAPTURE_DIR,
    patterns: Iterable[str] = DEFAULT_CAPTURE_PATTERNS,
    now: float | None = None,
) -> dict[str, Any]:
    current_ts = float(now) if now is not None else time.time()
    return _delete_old_files(
        directory=capture_dir,
        older_than_seconds=older_than_seconds,
        now=current_ts,
        patterns=patterns,
    ) | {"retention_seconds": float(older_than_seconds)}
