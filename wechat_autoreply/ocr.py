from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .paths import OCR_HELPER


def ocr_image(image_path: Path) -> list[dict]:
    proc = subprocess.run(
        ["swift", str(OCR_HELPER), str(image_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    payload = json.loads(proc.stdout)
    return list(payload.get("results", []))
