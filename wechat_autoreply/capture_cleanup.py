from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from .paths import CAPTURE_DIR


DEFAULT_CAPTURE_PATTERNS = ("wechat-roster-*.png", "wechat-chat-*.png")


def delete_capture_snapshots_older_than(
    *,
    older_than_seconds: float = 24 * 60 * 60,
    capture_dir: Path = CAPTURE_DIR,
    patterns: Iterable[str] = DEFAULT_CAPTURE_PATTERNS,
    now: float | None = None,
) -> dict[str, Any]:
    current_ts = float(now) if now is not None else time.time()
    deleted_count = 0
    deleted_bytes = 0
    deleted_paths: list[str] = []

    for pattern in patterns:
        for path in sorted(capture_dir.glob(pattern)):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            age_seconds = current_ts - float(stat.st_mtime)
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
        "retention_seconds": float(older_than_seconds),
    }
