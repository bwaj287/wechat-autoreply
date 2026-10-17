from __future__ import annotations

import json
from typing import Any


def load_first_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").lstrip()
    if not text:
        raise ValueError("empty JSON output")

    payload, _ = json.JSONDecoder().raw_decode(text)
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object output")
    return payload
