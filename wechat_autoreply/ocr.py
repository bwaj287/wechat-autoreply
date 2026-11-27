from __future__ import annotations

import stat
import json
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image

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


def ocr_image(image_path: Path, *, custom_words: tuple[str, ...] = ()) -> list[dict]:
    _ensure_tracked_helper_local()
    command = ["swift", str(OCR_HELPER), str(image_path)]
    if custom_words:
        command.append(json.dumps(custom_words, ensure_ascii=False))
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = str(exc.stderr or exc.stdout or exc).strip()[:500]
        raise RuntimeError(f"OCR failed for {image_path.name}: {detail}") from exc
    payload = load_first_json_object(proc.stdout)
    return list(payload.get("results", []))


def ocr_region(
    image_path: Path,
    region: tuple[float, float, float, float],
    *,
    custom_words: tuple[str, ...] = (),
) -> list[dict]:
    """Read a normalized top-left crop, returning boxes in the original image."""
    left, top, right, bottom = region
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("OCR region must be a nonempty normalized rectangle")
    with Image.open(image_path) as source:
        width, height = source.size
        box = (int(left * width), int(top * height), int(right * width), int(bottom * height))
        crop = source.crop(box)
        if min(crop.size) < 1:
            raise ValueError("OCR region is smaller than a pixel")
        crop = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
    with tempfile.TemporaryDirectory(prefix="wechat-ocr-region-") as tmp:
        path = Path(tmp) / "region.png"
        crop.save(path)
        observations = ocr_image(path, custom_words=custom_words)
    x0, y0, x1, y1 = box
    for obs in observations:
        bb = obs["bbox"]
        obs["bbox"] = {
            "x": (x0 + float(bb["x"]) * (x1 - x0)) / width,
            "y": (height - y1 + float(bb["y"]) * (y1 - y0)) / height,
            "w": float(bb["w"]) * (x1 - x0) / width,
            "h": float(bb["h"]) * (y1 - y0) / height,
        }
    return observations


def contact_label(observation: dict, contacts: list[str]) -> str:
    """Resolve only OCR-proposed names; never use this to rewrite message text."""
    raw = str(observation.get("text") or "")
    key = " ".join(raw.casefold().split())
    known = {" ".join(name.casefold().split()): name for name in contacts}
    if key in known:
        return raw
    # Service must not become an allowed contact through a lower-ranked candidate.
    stem = key.replace("...", "").replace("\u2026", "")
    if key.startswith("service") or (len(stem) >= 4 and "service".startswith(stem)):
        return raw
    matches = set()
    confidence = float(observation.get("confidence", 0.0))
    for candidate in observation.get("candidates", []):
        candidate_key = " ".join(str(candidate.get("text") or "").casefold().split())
        score = float(candidate.get("confidence", 0.0))
        if candidate_key in known and score >= max(0.5, confidence - 0.15):
            matches.add(known[candidate_key])
    if len(matches) == 1:
        match = matches.pop()
        if SequenceMatcher(None, key, " ".join(match.casefold().split())).ratio() >= 0.65:
            return match
    return raw
