from __future__ import annotations

import stat
import subprocess
import tempfile
from pathlib import Path

from .json_output import load_first_json_object
from .paths import OCR_HELPER, PROJECT_ROOT


def _is_dataless(path: Path) -> bool:
    try:
        flags = int(getattr(path.stat(), "st_flags", 0) or 0)
    except OSError:
        return False
    return bool(flags & int(getattr(stat, "SF_DATALESS", 0) or 0))


def _ensure_tracked_helper_local(
    helper_path: Path = OCR_HELPER,
    project_root: Path = PROJECT_ROOT,
) -> bool:
    if not _is_dataless(helper_path):
        return False

    relative_path = helper_path.relative_to(project_root).as_posix()
    restored = subprocess.run(
        ["git", "-C", str(project_root), "show", f"HEAD:{relative_path}"],
        capture_output=True,
        timeout=30,
        check=True,
    ).stdout
    mode = stat.S_IMODE(helper_path.stat().st_mode)
    with tempfile.NamedTemporaryFile(dir=helper_path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(restored)
    temp_path.chmod(mode)
    temp_path.replace(helper_path)
    return True


def ocr_image(image_path: Path) -> list[dict]:
    _ensure_tracked_helper_local()
    proc = subprocess.run(
        ["swift", str(OCR_HELPER), str(image_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    payload = load_first_json_object(proc.stdout)
    return list(payload.get("results", []))
