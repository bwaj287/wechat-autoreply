from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any


def load_first_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").lstrip()
    if not text:
        raise ValueError("empty JSON output")

    payload, _ = json.JSONDecoder().raw_decode(text)
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object output")
    return payload


def write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
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
