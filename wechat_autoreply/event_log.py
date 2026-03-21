import json
from datetime import datetime, timezone
from typing import Any

from .paths import EVENTS_PATH, ensure_runtime_dirs


def append_event(event_type: str, **payload: Any) -> None:
    ensure_runtime_dirs()
    event = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "type": event_type,
    }
    event.update(payload)
    with EVENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
