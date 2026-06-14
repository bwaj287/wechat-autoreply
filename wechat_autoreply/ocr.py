from __future__ import annotations

import subprocess
from pathlib import Path

from .json_output import load_first_json_object
from .paths import OCR_HELPER


def ocr_image(image_path: Path) -> list[dict]:
    proc = subprocess.run(
        ["swift", str(OCR_HELPER), str(image_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    payload = load_first_json_object(proc.stdout)
    return list(payload.get("results", []))
