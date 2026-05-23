from __future__ import annotations

import json
import os
import re
import tempfile
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .config_store import load_config, save_config
from .ocr import ocr_image
from .paths import BUBBLE_ROLE_HELPER, CAPTURE_DIR, PEEKABOO, WECHAT_APP
from .peekaboo_utils import peekaboo_commands, run, run_peekaboo_variants

IGNORE_TEXTS = {
    "Search",
    "Hold to Fn to use voice input",
}
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
DOCK_WECHAT_NAMES = ["WeChat", "Weixin"]
TEXT_SIGNAL_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")
EMOJI_CHAR_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u27BF"
    "]"
)
NON_TEXT_HINT_RE = re.compile(r"(表情|emoji|sticker|贴纸|动画表情|动画|emoticon)", re.IGNORECASE)
BRACKETED_HINT_RE = re.compile(r"^[\[\(（【<〈《].{1,12}[\]\)）】>〉》]$")
SHORT_PING_RE = re.compile(r"^[?？!！]{1,3}$")
MESSAGE_BANNER_RE = re.compile(
    r"(?i)(?:^|\b)\d+\s*new\s*message(?:s)?\b|(?:^|\b)new\s*message(?:s)?\b"
)
SYSTEM_CALL_PREVIEW_RE = re.compile(
    r"(?i)^(?:video\s*call|voice\s*call|call\s*not\s*answered|already\s*answered\s*elsewhere|call\s*canceled\s*by\s*caller)$"
)
MEDIA_PLACEHOLDER_RE = re.compile(
    r"(?i)(?:^\s*[\[\(（【<〈《]?\s*(?:photo|picture|image|pic|sticker|emoji|video|图片|照片|相片|截图|表情包|贴纸|视频)"
    r"(?:[^\]\)）】>〉》]{0,12})?[\]\)）】>〉》]?\s*$)"
)
MIN_ROSTER_WINDOW_WIDTH = 720
MIN_ROSTER_WINDOW_HEIGHT = 620
TARGET_ROSTER_WINDOW_X = 96
TARGET_ROSTER_WINDOW_Y = 84
TARGET_ROSTER_WINDOW_WIDTH = 980
TARGET_ROSTER_WINDOW_HEIGHT = 820


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def normalize_name_for_match(text: str) -> str:
    value = normalize_text(text)
    return value.translate(str.maketrans({"0": "o"}))


def _canonical_name_key(text: str) -> str:
    value = normalize_name_for_match(text)
    if not value:
        return ""
    value = value.replace("…", "").replace("...", "")
    value = re.sub(
        r"(?i)\b(?:today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"mon|tue|wed|thu|fri|sat|sun)\b.*$|(?:今天|昨天|前天).*$",
        "",
        value,
    ).strip()
    value = re.sub(r"\b\d{1,2}:\d{2}\b$", "", value).strip()
    value = re.sub(r"\(\s*\d+\s*\)\s*$", "", value).strip()
    value = re.sub(r"^[^0-9a-z\u4e00-\u9fff]+|[^0-9a-z\u4e00-\u9fff]+$", "", value).strip()
    return " ".join(value.split())


def _looks_truncated_name(text: str) -> bool:
    value = str(text or "")
    return "..." in value or "…" in value


def names_match(a: str, b: str) -> bool:
    an = _canonical_name_key(a)
    bn = _canonical_name_key(b)
    if not an or not bn:
        return False
    if an == bn:
        return True
    # Only allow prefix fallback when OCR truncation is visible.
    if _looks_truncated_name(a) or _looks_truncated_name(b):
        shorter, longer = (an, bn) if len(an) <= len(bn) else (bn, an)
        return len(shorter) >= 2 and longer.startswith(shorter)
    return False


def has_text_signal(text: str) -> bool:
    return bool(TEXT_SIGNAL_RE.search((text or "").strip()))


def is_nontext_message(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    if NON_TEXT_HINT_RE.search(value):
        return True
    if BRACKETED_HINT_RE.fullmatch(value) and not has_text_signal(value):
        return True
    return bool(EMOJI_CHAR_RE.search(value))


def is_media_message(text: str) -> bool:
    value = (text or "").strip()
    if not value or _looks_like_system_call_preview(value):
        return False
    if MEDIA_PLACEHOLDER_RE.search(value):
        return True
    if NON_TEXT_HINT_RE.search(value):
        return True
    return bool(BRACKETED_HINT_RE.fullmatch(value) and not has_text_signal(value))


def has_meaningful_text(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    if SHORT_PING_RE.fullmatch(value):
        return True
    return has_text_signal(value) or is_nontext_message(value)


def has_signal_text(text: str) -> bool:
    return has_meaningful_text(text)


def _looks_like_message_banner_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    normalized = normalize_text(value)
    return bool(MESSAGE_BANNER_RE.search(normalized))


def _sanitize_chat_title_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if _looks_like_message_banner_text(value):
        return ""
    return value


def _looks_like_system_call_preview(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(SYSTEM_CALL_PREVIEW_RE.fullmatch(value))


def _click_dock_item(name: str) -> bool:
    script = f"""
tell application "System Events"
  tell process "Dock"
    try
      click UI element "{name}" of list 1
      return "clicked"
    on error
      return "missing"
    end try
  end tell
end tell
"""
    out = run(["osascript", "-e", script], timeout=30).stdout.strip().lower()
    return out == "clicked"


def activate_wechat() -> None:
    for name in DOCK_WECHAT_NAMES:
        if _click_dock_item(name):
            time.sleep(0.5)
            return
    run(["osascript", "-e", 'tell application "WeChat" to activate', "-e", "delay 0.4"], timeout=30)


def _escape_applescript_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def capture_frontmost_app() -> str:
    script = """
tell application "System Events"
  try
    return name of first process whose frontmost is true
  on error
    return ""
  end try
end tell
"""
    try:
        return run(["osascript", "-e", script], timeout=30).stdout.strip()
    except Exception:
        return ""


def restore_frontmost_app(app_name: str) -> None:
    target = str(app_name or "").strip()
    if not target or target in {"WeChat", "Weixin"}:
        return
    run(["osascript", "-e", f'tell application "{_escape_applescript_string(target)}" to activate'], timeout=30)


def _process_visible(name: str) -> bool | None:
    target = str(name or "").strip()
    if not target:
        return None
    script = f'''
tell application "System Events"
  if exists process "{_escape_applescript_string(target)}" then
    return visible of process "{_escape_applescript_string(target)}"
  end if
end tell
return "missing"
'''
    try:
        out = run(["osascript", "-e", script], timeout=30).stdout.strip().lower()
    except Exception:
        return None
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def _any_wechat_visible() -> bool:
    seen = False
    for name in DOCK_WECHAT_NAMES:
        visible = _process_visible(name)
        if visible is None:
            continue
        seen = True
        if visible:
            return True
    return False


def hide_wechat() -> None:
    scripts = [
        """
tell application "System Events"
  if exists process "WeChat" then
    set visible of process "WeChat" to false
    return "hidden"
  end if
end tell
return "missing"
""",
        """
tell application "System Events"
  if exists process "Weixin" then
    set visible of process "Weixin" to false
    return "hidden"
  end if
end tell
return "missing"
""",
        """
tell application "WeChat" to hide
return "hidden"
""",
        """
tell application "Weixin" to hide
return "hidden"
""",
        """
tell application "System Events"
  keystroke "h" using {command down}
end tell
return "hidden"
""",
    ]
    errors: list[str] = []
    for script in scripts:
        try:
            run(["osascript", "-e", script], timeout=30)
            time.sleep(0.12)
            if not _any_wechat_visible():
                return
        except Exception as exc:  # pragma: no cover - exercised only on macOS host
            errors.append(str(exc))
    raise RuntimeError(" ; ".join(errors) or "failed to hide wechat")


def _read_clipboard_text() -> str:
    try:
        return run(["pbpaste"], check=False, timeout=5).stdout
    except Exception:
        return ""


def _write_clipboard_text(text: str) -> None:
    try:
        subprocess.run(
            ["pbcopy"],
            input=str(text),
            text=True,
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def list_wechat_windows() -> list[dict[str, Any]]:
    payload = json.loads(
        run_peekaboo_variants(
            peekaboo_commands(["list", "windows", "--app", WECHAT_APP, "--json"]),
            timeout=120,
        ).stdout
    )
    windows: list[dict[str, Any]] = []
    for window in payload.get("data", {}).get("windows", []):
        bounds = window.get("bounds") or []
        if len(bounds) != 2:
            continue
        (x, y), (width, height) = bounds
        if width < 50 or height < 50:
            continue
        windows.append(
            {
                "title": (window.get("title") or "").strip(),
                "x": int(x),
                "y": int(y),
                "width": int(width),
                "height": int(height),
                "window_id": int(window.get("window_id", 0) or 0),
                "index": int(window.get("index", 0) or 0),
                "isMainWindow": bool(window.get("isMainWindow", False)),
            }
        )
    return windows


def choose_roster_window(windows: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [window for window in windows if window["title"] == "Weixin"]
    if not candidates:
        raise RuntimeError(f"no roster window found: {windows!r}")
    return max(candidates, key=lambda item: item["width"] * item["height"])


def _sanitize_window_bounds(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    try:
        x = int(value.get("x", 0) or 0)
        y = int(value.get("y", 0) or 0)
        width = int(value.get("width", 0) or 0)
        height = int(value.get("height", 0) or 0)
    except Exception:
        return {}
    if width < 200 or height < 200:
        return {}
    return {"x": x, "y": y, "width": width, "height": height}


def _preferred_roster_window_bounds() -> dict[str, int]:
    try:
        config = load_config()
    except Exception:
        return {}
    return _sanitize_window_bounds(config.get("preferred_roster_window_bounds", {}))


def _remember_roster_window_bounds(info: dict[str, Any]) -> None:
    if int(info.get("width", 0) or 0) < MIN_ROSTER_WINDOW_WIDTH:
        return
    if int(info.get("height", 0) or 0) < MIN_ROSTER_WINDOW_HEIGHT:
        return
    current = _sanitize_window_bounds(info)
    if not current:
        return
    try:
        config = load_config()
    except Exception:
        return
    previous = _sanitize_window_bounds(config.get("preferred_roster_window_bounds", {}))
    if previous == current:
        return
    config["preferred_roster_window_bounds"] = current
    try:
        save_config(config)
    except Exception:
        return


def _focus_window(info: dict[str, Any]) -> None:
    window_id = int(info.get("window_id", 0) or 0)
    if window_id <= 0:
        return
    binary = str(PEEKABOO) if PEEKABOO.exists() else "peekaboo"
    commands = [
        [binary, "window", "focus", "--app", WECHAT_APP, "--window-id", str(window_id), "--json"],
        [binary, "window", "focus", "--app", "Weixin", "--window-id", str(window_id), "--json"],
    ]
    errors: list[str] = []
    for cmd in commands:
        try:
            run(cmd, timeout=30)
            time.sleep(0.2)
            return
        except Exception as exc:  # pragma: no cover - exercised only on macOS host
            errors.append(str(exc))
    for app_name in (WECHAT_APP, "Weixin"):
        try:
            run(["osascript", "-e", f'tell application "{_escape_applescript_string(app_name)}" to activate'], timeout=30)
            time.sleep(0.2)
            return
        except Exception as exc:  # pragma: no cover - exercised only on macOS host
            errors.append(str(exc))
    raise RuntimeError(" ; ".join(errors) or "failed to focus WeChat window")


def _resize_frontmost_wechat_window(
    *,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> None:
    preferred = _preferred_roster_window_bounds()
    x = int(preferred.get("x", TARGET_ROSTER_WINDOW_X) if x is None else x)
    y = int(preferred.get("y", TARGET_ROSTER_WINDOW_Y) if y is None else y)
    width = int(preferred.get("width", TARGET_ROSTER_WINDOW_WIDTH) if width is None else width)
    height = int(preferred.get("height", TARGET_ROSTER_WINDOW_HEIGHT) if height is None else height)
    scripts = [
        f'''
tell application "System Events"
  if exists process "WeChat" then
    tell process "WeChat"
      if exists front window then
        set position of front window to {{{x}, {y}}}
        set size of front window to {{{width}, {height}}}
        return "ok"
      end if
    end tell
  end if
end tell
return "missing"
''',
        f'''
tell application "System Events"
  if exists process "Weixin" then
    tell process "Weixin"
      if exists front window then
        set position of front window to {{{x}, {y}}}
        set size of front window to {{{width}, {height}}}
        return "ok"
      end if
    end tell
  end if
end tell
return "missing"
''',
    ]
    errors: list[str] = []
    for script in scripts:
        try:
            result = run(["osascript", "-e", script], timeout=30).stdout.strip().lower()
            if result in {"ok", "missing", ""}:
                time.sleep(0.35)
                return
        except Exception as exc:  # pragma: no cover - exercised only on macOS host
            errors.append(str(exc))
    if errors:
        raise RuntimeError(" ; ".join(errors))


def ensure_main_roster_window() -> dict[str, Any]:
    windows = list_wechat_windows()
    roster = choose_roster_window(windows)
    _focus_window(roster)
    if roster["width"] >= MIN_ROSTER_WINDOW_WIDTH and roster["height"] >= MIN_ROSTER_WINDOW_HEIGHT:
        _remember_roster_window_bounds(roster)
        return roster
    _resize_frontmost_wechat_window()
    windows = list_wechat_windows()
    roster = choose_roster_window(windows)
    _focus_window(roster)
    _remember_roster_window_bounds(roster)
    return roster


def choose_chat_window(windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [
        window
        for window in windows
        if str(window.get("title") or "").strip()
        and window["title"] not in {"Weixin", "WeChat (Window)"}
    ]
    if not usable:
        return None
    max_area = max(window["width"] * window["height"] for window in usable)
    min_area = max(55_000, int(max_area * 0.09))
    min_side = max(180, int((max_area**0.5) * 0.28))
    candidates = [
        window
        for window in usable
        if window["width"] * window["height"] >= min_area and min(window["width"], window["height"]) >= min_side
    ]
    if not candidates:
        candidates = usable
    if not candidates:
        return None
    main_candidates = [window for window in candidates if window.get("isMainWindow")]
    if main_candidates:
        return max(main_candidates, key=lambda item: item["width"] * item["height"])
    return max(candidates, key=lambda item: item["width"] * item["height"])


def capture_window(path: Path, info: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "image",
        "--app",
        WECHAT_APP,
        "--mode",
        "window",
        "--path",
        str(path),
        "--json",
    ]
    if info.get("window_id"):
        args.extend(["--window-id", str(info["window_id"])])
    else:
        args.extend(["--window-title", info["title"]])
    run_peekaboo_variants(peekaboo_commands(args), timeout=120)
    return path


def _is_time(text: str) -> bool:
    return bool(TIME_RE.fullmatch(text.strip().rstrip(".")))


def _global_coords(info: dict[str, Any], obs: dict[str, Any]) -> tuple[int, int]:
    bbox = obs["bbox"]
    gx = info["x"] + (float(bbox["x"]) + float(bbox["w"]) / 2.0) * info["width"]
    gy = info["y"] + (1.0 - (float(bbox["y"]) + float(bbox["h"]) / 2.0)) * info["height"]
    return int(round(gx)), int(round(gy))


def _cluster_rows(items: list[dict[str, Any]], threshold: float = 0.06) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda candidate: candidate["center"]["y"]):
        cy = item["center"]["y"]
        placed = False
        for row in rows:
            if abs(row["cy"] - cy) <= threshold:
                row["items"].append(item)
                row["cy"] = sum(entry["center"]["y"] for entry in row["items"]) / len(row["items"])
                placed = True
                break
        if not placed:
            rows.append({"cy": cy, "items": [item]})
    return rows


def _median_gap(values: list[float], min_gap: float, max_gap: float, default: float) -> float:
    if len(values) < 2:
        return default
    diffs = [values[i + 1] - values[i] for i in range(len(values) - 1) if min_gap <= values[i + 1] - values[i] <= max_gap]
    if not diffs:
        return default
    diffs.sort()
    mid = len(diffs) // 2
    if len(diffs) % 2 == 1:
        return diffs[mid]
    return (diffs[mid - 1] + diffs[mid]) / 2.0


def _adaptive_row_threshold(items: list[dict[str, Any]], default: float = 0.06) -> float:
    ys = sorted(float(item.get("center", {}).get("y", 0.0) or 0.0) for item in items)
    median_gap = _median_gap(ys, min_gap=0.012, max_gap=0.25, default=default / 0.58)
    # Keep a slightly higher floor so name + preview lines from the same
    # chat cell are clustered together instead of split into fake contacts.
    return max(0.04, min(0.09, median_gap * 0.58))


def _roster_band_profiles(window_info: dict[str, Any]) -> list[tuple[float, float, float]]:
    width = float(window_info.get("width", 0) or 0)
    # Compact windows push roster text slightly right; wider windows shift slightly left.
    t = 0.0
    if width > 0:
        t = max(0.0, min(1.0, (width - 520.0) / 680.0))
    primary = (0.12 - 0.06 * t, 0.38 - 0.08 * t, 0.025 - 0.01 * t)
    fallback = (max(0.03, primary[0] - 0.05), min(0.45, primary[1] + 0.07), max(0.012, primary[2] - 0.01))
    return [primary, fallback]


def _build_roster_ocr_variants(image_path: Path) -> list[Path]:
    variants: list[Path] = []
    with Image.open(image_path) as source:
        rgb = source.convert("RGB")
        upscaled = rgb.resize(
            (max(2, rgb.width * 2), max(2, rgb.height * 2)),
            resample=Image.Resampling.BICUBIC,
        )
        grayscale = ImageOps.grayscale(upscaled)
        contrast = ImageOps.autocontrast(grayscale, cutoff=2)
        inverted = ImageOps.invert(contrast)
        for image in (contrast, inverted):
            with tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False) as handle:
                path = Path(handle.name)
            image.save(path, format="PNG")
            variants.append(path)
    return variants


def _prepare_ocr_items(image_path: Path, *, panel_hint: str = "") -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    temp_variants: list[Path] = []
    source_images: list[Path] = [image_path]
    if panel_hint == "roster":
        try:
            temp_variants = _build_roster_ocr_variants(image_path)
            source_images.extend(temp_variants)
        except Exception:
            temp_variants = []
    def _find_bbox_match(x: float, top: float, w: float, h: float) -> dict[str, Any] | None:
        for existing in prepared:
            eb = existing.get("bbox", {})
            if (
                abs(float(eb.get("x", 0.0)) - x) <= 0.022
                and abs(float(eb.get("top", 0.0)) - top) <= 0.022
                and abs(float(eb.get("w", 0.0)) - w) <= 0.035
                and abs(float(eb.get("h", 0.0)) - h) <= 0.035
            ):
                return existing
        return None

    def _record_item(text: str, x: float, y: float, w: float, h: float, confidence: float) -> None:
        top = 1.0 - (y + h)
        prepared.append(
            {
                "text": text,
                "confidence": confidence,
                "bbox": {"x": x, "y": y, "w": w, "h": h, "top": top, "left": x},
                "center": {"x": x + w / 2.0, "y": top + h / 2.0},
            }
        )

    try:
        # Base OCR pass: keep all valid observations.
        for item in ocr_image(image_path):
            text = (item.get("text") or "").strip()
            if not text:
                continue
            bbox = item.get("bbox") or {}
            x = float(bbox.get("x", 0.0))
            y = float(bbox.get("y", 0.0))
            w = float(bbox.get("w", 0.0))
            h = float(bbox.get("h", 0.0))
            confidence = float(item.get("confidence", 0.0))
            _record_item(text, x, y, w, h, confidence)

        # Enhanced roster OCR pass:
        # only update existing boxes, never add brand-new boxes.
        # This avoids false rows introduced by aggressive enhancement.
        for source in source_images[1:]:
            for item in ocr_image(source):
                text = (item.get("text") or "").strip()
                if not text:
                    continue
                bbox = item.get("bbox") or {}
                x = float(bbox.get("x", 0.0))
                y = float(bbox.get("y", 0.0))
                w = float(bbox.get("w", 0.0))
                h = float(bbox.get("h", 0.0))
                top = 1.0 - (y + h)
                confidence = float(item.get("confidence", 0.0))
                duplicate = _find_bbox_match(x, top, w, h)
                if duplicate is None:
                    continue
                existing_conf = float(duplicate.get("confidence", 0.0) or 0.0)
                existing_text = str(duplicate.get("text", "") or "").strip()
                if confidence > existing_conf + 0.04 or (
                    len(normalize_text(text)) > len(normalize_text(existing_text))
                    and confidence >= existing_conf - 0.02
                ):
                    duplicate.update(
                        {
                            "text": text,
                            "confidence": confidence,
                            "bbox": {"x": x, "y": y, "w": w, "h": h, "top": top, "left": x},
                            "center": {"x": x + w / 2.0, "y": top + h / 2.0},
                        }
                    )
    finally:
        for path in temp_variants:
            path.unlink(missing_ok=True)
    _annotate_bubble_roles(prepared, image_path)
    return prepared


def _annotate_bubble_roles(items: list[dict[str, Any]], image_path: Path) -> None:
    if not items:
        return
    payload = [
        {
            "index": index,
            "x": float(item["bbox"].get("x", 0.0)),
            "y": float(item["bbox"].get("y", 0.0)),
            "w": float(item["bbox"].get("w", 0.0)),
            "h": float(item["bbox"].get("h", 0.0)),
        }
        for index, item in enumerate(items)
    ]
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        tmp_path = Path(handle.name)
    try:
        proc = subprocess.run(
            ["swift", str(BUBBLE_ROLE_HELPER), str(image_path), str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        role_payload = json.loads(proc.stdout)
    except Exception:
        return
    finally:
        tmp_path.unlink(missing_ok=True)
    for role_item in role_payload.get("items", []):
        index = int(role_item.get("index", -1))
        if 0 <= index < len(items):
            items[index]["bubbleRole"] = str(role_item.get("role", "unknown"))
            items[index]["greenPixels"] = int(role_item.get("greenPixels", 0))
            items[index]["grayPixels"] = int(role_item.get("grayPixels", 0))


def extract_visible_chats(obs_list: list[dict[str, Any]], window_info: dict[str, Any]) -> list[dict[str, Any]]:
    ignored = {normalize_text(item) for item in IGNORE_TEXTS}

    def parse_chats(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        threshold = _adaptive_row_threshold(candidates, default=0.06)
        chats: list[dict[str, Any]] = []
        for row in _cluster_rows(candidates, threshold=threshold):
            items = sorted(row["items"], key=lambda entry: (entry["bbox"]["top"], entry["bbox"]["left"]))
            name_item = None
            preview_item = None
            time_item = None
            for item in items:
                if _is_time(item["text"]):
                    time_item = item
                    continue
                if not name_item:
                    name_item = item
                elif not preview_item and item["text"] != name_item["text"]:
                    preview_item = item
            if not name_item:
                continue
            if (
                preview_item
                and _looks_like_system_call_preview(name_item["text"])
                and not _looks_like_system_call_preview(preview_item["text"])
            ):
                # OCR occasionally orders the call-status preview above the real
                # contact name, which makes rows like `1ock / Video Call` look
                # like the contact itself is named "Video Call". Prefer the
                # non-call text as the contact label in that case.
                name_item, preview_item = preview_item, name_item
            if len(name_item["text"]) <= 1 and not re.search(r"[A-Za-z\u4e00-\u9fff]", name_item["text"]):
                continue
            gx, gy = _global_coords(window_info, name_item)
            chats.append(
                {
                    "name": name_item["text"],
                    "preview": preview_item["text"] if preview_item else "",
                    "time": time_item["text"] if time_item else "",
                    "ocrTop": round(name_item["bbox"]["top"], 4),
                    "ocrLeft": round(name_item["bbox"]["left"], 4),
                    "click": {"x": gx, "y": gy},
                }
            )
        chats.sort(key=lambda item: item["ocrTop"])
        if not chats:
            return chats
        near_dup_gap = max(
            0.03,
            min(0.07, _median_gap([item["ocrTop"] for item in chats], min_gap=0.018, max_gap=0.25, default=0.10) * 0.62),
        )
        cleaned: list[dict[str, Any]] = []
        for chat in chats:
            if cleaned:
                prev = cleaned[-1]
                gap = float(chat.get("ocrTop", 0.0)) - float(prev.get("ocrTop", 0.0))
                left_gap = abs(float(chat.get("ocrLeft", 0.0) or 0.0) - float(prev.get("ocrLeft", 0.0) or 0.0))
                split_line_gap = max(0.06, min(0.09, near_dup_gap * 1.55))
                # OCR may split one chat row into two stacked lines (name + preview line).
                # Merge them back so unread badge attaches to the real contact row.
                if (
                    gap <= split_line_gap
                    and left_gap <= 0.03
                    and not str(chat.get("time") or "").strip()
                    and not str(chat.get("preview") or "").strip()
                ):
                    extra_preview = str(chat.get("name") or "").strip()
                    if extra_preview:
                        prev_preview = str(prev.get("preview") or "").strip()
                        if not prev_preview:
                            prev["preview"] = extra_preview
                        elif normalize_text(prev_preview) != normalize_text(extra_preview):
                            prev["preview"] = _join_texts([prev_preview, extra_preview])
                    continue
                if gap < near_dup_gap:
                    prev_score = int(bool(prev.get("time"))) + int(bool(prev.get("preview")))
                    cur_score = int(bool(chat.get("time"))) + int(bool(chat.get("preview")))
                    same_name = normalize_text(chat["name"]) == normalize_text(prev.get("name", ""))
                    preview_split = normalize_text(chat["name"]) == normalize_text(prev.get("preview", ""))
                    # Keep the richer row when OCR accidentally splits one chat cell.
                    if same_name or preview_split or cur_score <= prev_score:
                        continue
                    cleaned[-1] = chat
                    continue
            cleaned.append(chat)
        return cleaned

    for min_left, max_left, min_width in _roster_band_profiles(window_info):
        candidates: list[dict[str, Any]] = []
        for obs in obs_list:
            left = obs["bbox"]["left"]
            top = obs["bbox"]["top"]
            width = obs["bbox"]["w"]
            if left < min_left or left > max_left:
                continue
            if top < 0.08 or top > 0.985:
                continue
            if width < min_width:
                continue
            if normalize_text(obs["text"]) in ignored:
                continue
            candidates.append(obs)
        chats = parse_chats(candidates)
        if chats:
            return chats
    return []


def annotate_unread_chats(
    chats: list[dict[str, Any]],
    roster_path: Path,
    *,
    fallback_target_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not chats:
        return chats
    rows: list[dict[str, Any]] = []
    tops = [chat["ocrTop"] for chat in chats]
    median_row_gap = _median_gap(tops, min_gap=0.018, max_gap=0.28, default=0.11)
    head_pad = max(0.045, min(0.11, median_row_gap * 0.62))
    tail_pad = max(0.07, min(0.17, median_row_gap * 0.95))
    min_span = max(0.05, min(0.16, median_row_gap * 0.72))
    for index, chat in enumerate(chats):
        current = tops[index]
        prev_mid = (tops[index - 1] + current) / 2.0 if index > 0 else max(0.015, current - head_pad * 1.2)
        next_mid = (current + tops[index + 1]) / 2.0 if index + 1 < len(tops) else min(0.98, current + tail_pad)
        if next_mid - prev_mid < min_span:
            grow = (min_span - (next_mid - prev_mid)) / 2.0
            prev_mid = max(0.02, prev_mid - grow)
            next_mid = min(0.99, next_mid + grow)
        rows.append(
            {
                "index": index,
                "name": chat["name"],
                "rowTop": round(prev_mid, 4),
                "rowBottom": round(next_mid, 4),
                "nameLeft": float(chat.get("ocrLeft", 0.15) or 0.15),
            }
        )
    try:
        with Image.open(roster_path) as roster_image:
            rgb = roster_image.convert("RGB")
            for index, chat in enumerate(chats):
                direct = _fallback_row_badge_detection(rgb, rows[index])
                if not direct:
                    chat["redPixelCount"] = 0
                    chat["digitPixelCount"] = 0
                    chat["numericBadge"] = False
                    chat["unread"] = False
                    continue
                chat["redPixelCount"] = int(direct.get("redPixelCount", 0))
                chat["digitPixelCount"] = int(direct.get("digitPixelCount", 0))
                chat["numericBadge"] = bool(direct.get("numericBadge", False))
                chat["unread"] = bool(direct.get("unread", False))
    except Exception:
        for chat in chats:
            chat["redPixelCount"] = 0
            chat["digitPixelCount"] = 0
            chat["numericBadge"] = False
            chat["unread"] = False
    return chats


def _is_red_badge_pixel(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    max_v = max(r, g, b)
    min_v = min(r, g, b)
    if max_v <= 92:
        return False
    saturation = (max_v - min_v) / max_v if max_v else 0.0
    return saturation > 0.20 and r > g + 28 and r > b + 28


def _is_white_digit_pixel(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    max_v = max(r, g, b)
    min_v = min(r, g, b)
    luminance = (r + g + b) / 3.0
    saturation = (max_v - min_v) / max_v if max_v else 0.0
    return luminance > 205 and saturation < 0.34


def _count_light_pixels(image: Image.Image, threshold: int) -> int:
    pixels = image.load()
    width, height = image.size
    count = 0
    for y in range(height):
        for x in range(width):
            if int(pixels[x, y]) >= threshold:
                count += 1
    return count


def _count_dark_pixels(image: Image.Image, threshold: int) -> int:
    pixels = image.load()
    width, height = image.size
    count = 0
    for y in range(height):
        for x in range(width):
            if int(pixels[x, y]) <= threshold:
                count += 1
    return count


def _enhanced_digit_equivalent(crop: Image.Image) -> int:
    grayscale = ImageOps.autocontrast(ImageOps.grayscale(crop), cutoff=2)
    inverted = ImageOps.invert(grayscale)
    scale = 8
    gray_up = grayscale.resize((grayscale.width * scale, grayscale.height * scale), Image.Resampling.BICUBIC)
    inverted_up = inverted.resize((inverted.width * scale, inverted.height * scale), Image.Resampling.BICUBIC)
    # Use a high threshold so we only keep the brightest digit strokes after
    # enhancement. Red-heavy avatars produce much larger bright regions here and
    # will be rejected by the upper bound in the caller.
    light_equivalent = _count_light_pixels(gray_up, 220) // (scale * scale)
    dark_equivalent = _count_dark_pixels(inverted_up, 35) // (scale * scale)
    return min(light_equivalent, dark_equivalent)


def _connected_components(mask: list[list[bool]]) -> list[dict[str, int]]:
    if not mask or not mask[0]:
        return []
    height = len(mask)
    width = len(mask[0])
    visited = [[False] * width for _ in range(height)]
    components: list[dict[str, int]] = []
    for y in range(height):
        for x in range(width):
            if visited[y][x] or not mask[y][x]:
                continue
            stack = [(x, y)]
            visited[y][x] = True
            count = 0
            min_x = max_x = x
            min_y = max_y = y
            while stack:
                cx, cy = stack.pop()
                count += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nx = cx + dx
                    ny = cy + dy
                    if 0 <= nx < width and 0 <= ny < height and not visited[ny][nx] and mask[ny][nx]:
                        visited[ny][nx] = True
                        stack.append((nx, ny))
            components.append(
                {
                    "count": count,
                    "minX": min_x,
                    "maxX": max_x,
                    "minY": min_y,
                    "maxY": max_y,
                    "width": max_x - min_x + 1,
                    "height": max_y - min_y + 1,
                }
            )
    return components


def _fallback_row_badge_detection(image: Image.Image, row: dict[str, Any]) -> dict[str, Any] | None:
    width, height = image.size
    row_top = float(row.get("rowTop", 0.0) or 0.0)
    row_bottom = float(row.get("rowBottom", 0.0) or 0.0)
    name_left = float(row.get("nameLeft", 0.15) or 0.15)
    row_span = max(0.02, row_bottom - row_top)
    # Tight badge window: right beside the contact name, only across the upper
    # portion of the row. This avoids merging the badge with red-heavy avatars.
    scan_pad_left = max(0.018, min(0.022, row_span * 0.16))
    scan_pad_right = max(0.018, min(0.024, row_span * 0.18))
    x0 = max(0, int(round((name_left - scan_pad_left) * width)))
    x1 = min(width, int(round((name_left + scan_pad_right) * width)))
    y0 = max(0, int(round(max(0.0, row_top - 0.012) * height)))
    y1 = min(height, int(round(min(1.0, row_top + max(0.085, min(0.135, row_span * 0.78))) * height)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    region_h = max(1, y1 - y0)
    mask: list[list[bool]] = []
    for py in range(y0, y1):
        row_mask: list[bool] = []
        for px in range(x0, x1):
            row_mask.append(_is_red_badge_pixel(image.getpixel((px, py))))
        mask.append(row_mask)
    best: dict[str, int] | None = None
    for component in _connected_components(mask):
        comp_w = int(component["width"])
        comp_h = int(component["height"])
        count = int(component["count"])
        if count < 20 or count > 320:
            continue
        if comp_w < 9 or comp_w > 20 or comp_h < 9 or comp_h > 28:
            continue
        fill_ratio = count / max(1, comp_w * comp_h)
        aspect = comp_w / max(1, comp_h)
        center_y = ((component["minY"] + component["maxY"]) / 2.0) / region_h
        if fill_ratio < 0.16 or fill_ratio > 0.86:
            continue
        if aspect < 0.45 or aspect > 1.70:
            continue
        if center_y < 0.08 or center_y > 0.92:
            continue
        if best is None or count > int(best["count"]):
            best = component
    if best is None:
        return None
    pad_x = max(1, int(best["width"] * 0.16))
    pad_y = max(1, int(best["height"] * 0.16))
    ix0 = x0 + int(best["minX"]) + pad_x
    ix1 = x0 + int(best["maxX"]) - pad_x + 1
    iy0 = y0 + int(best["minY"]) + pad_y
    iy1 = y0 + int(best["maxY"]) - pad_y + 1
    if ix1 <= ix0 or iy1 <= iy0:
        return None
    digit_pixels = 0
    for py in range(iy0, iy1):
        for px in range(ix0, ix1):
            if _is_white_digit_pixel(image.getpixel((px, py))):
                digit_pixels += 1
    if digit_pixels < 10:
        crop = image.crop((ix0, iy0, ix1, iy1))
        enhanced_equivalent = _enhanced_digit_equivalent(crop)
        if digit_pixels >= 8 and 3 <= enhanced_equivalent <= 10:
            digit_pixels = max(digit_pixels, enhanced_equivalent)
        else:
            return None
    return {
        "redPixelCount": int(best["count"]),
        "digitPixelCount": int(digit_pixels),
        "numericBadge": True,
        "unread": True,
    }


def find_chat(chats: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    target_key = _canonical_name_key(target)
    if target_key:
        for chat in chats:
            if _canonical_name_key(str(chat.get("name", ""))) == target_key:
                return chat
    for chat in chats:
        if names_match(chat["name"], target):
            return chat
    return None


def click_coords(
    x: int,
    y: int,
    *,
    window_id: int | None = None,
    auto_focus: bool = True,
) -> None:
    args = ["click", "--coords", f"{x},{y}", "--app", WECHAT_APP]
    if window_id and int(window_id) > 0:
        args.extend(["--window-id", str(int(window_id))])
    if not auto_focus:
        args.append("--no-auto-focus")
    args.append("--json")
    run_peekaboo_variants(peekaboo_commands(args), timeout=60)


def _pick_selected_title(obs_list: list[dict[str, Any]]) -> str:
    candidates: list[dict[str, Any]] = []
    ignored = {normalize_text(item) for item in IGNORE_TEXTS}
    for obs in obs_list:
        text = obs["text"]
        left = obs["bbox"]["left"]
        top = obs["bbox"]["top"]
        width = obs["bbox"]["w"]
        # Only trust the actual chat-title strip at the upper-left of the right panel.
        # This excludes the green "new message(s)" banner on the upper-right.
        if left < 0.33 or left > 0.62 or top > 0.09 or width < 0.02:
            continue
        if _is_time(text):
            continue
        if normalize_text(text) in ignored:
            continue
        if _looks_like_message_banner_text(text):
            continue
        candidates.append(obs)
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item["bbox"]["top"], -item["bbox"]["w"]))
    return _sanitize_chat_title_text(candidates[0]["text"])


def _pick_chat_window_title(obs_list: list[dict[str, Any]]) -> str:
    candidates: list[dict[str, Any]] = []
    ignored = {normalize_text(item) for item in IGNORE_TEXTS}
    for obs in obs_list:
        text = obs["text"]
        left = obs["bbox"]["left"]
        top = obs["bbox"]["top"]
        width = obs["bbox"]["w"]
        if top > 0.12 or left < 0.03 or left > 0.42 or width < 0.02:
            continue
        if _is_time(text):
            continue
        if normalize_text(text) in ignored:
            continue
        if _looks_like_message_banner_text(text):
            continue
        candidates.append(obs)
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item["bbox"]["top"], item["bbox"]["left"]))
    return _sanitize_chat_title_text(candidates[0]["text"])


def _join_texts(parts: list[str]) -> str:
    if not parts:
        return ""
    return "".join(parts) if any(re.search(r"[\u4e00-\u9fff]", part) for part in parts) else " ".join(parts)


def _collapse_panel_lines(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return []
    clustered_items = [
        {
            "text": item["text"],
            "bbox": {"top": item["top"], "left": item["left"], "w": item["width"]},
            "center": {"y": item["top"] + 0.01},
            "confidence": float(item.get("confidence", 0.0) or 0.0),
            "greenPixels": int(item.get("greenPixels", 0) or 0),
            "grayPixels": int(item.get("grayPixels", 0) or 0),
            "messageKind": str(item.get("messageKind", "text") or "text"),
        }
        for item in items
    ]
    rows = _cluster_rows(clustered_items, threshold=_adaptive_row_threshold(clustered_items, default=0.07))
    collapsed: list[dict[str, Any]] = []
    for row in rows:
        row_items = sorted(row["items"], key=lambda item: (item["bbox"]["top"], item["bbox"]["left"]))
        collapsed.append(
            {
                "text": _join_texts([item["text"] for item in row_items]),
                "top": round(min(item["bbox"]["top"] for item in row_items), 4),
                "left": round(min(item["bbox"]["left"] for item in row_items), 4),
                "width": round(max(item["bbox"]["w"] for item in row_items), 4),
                "confidence": round(
                    (
                        sum(float(item.get("confidence", 0.0) or 0.0) for item in row_items)
                        / max(1, len(row_items))
                    ),
                    4,
                ),
                "greenPixels": int(sum(int(item.get("greenPixels", 0) or 0) for item in row_items)),
                "grayPixels": int(sum(int(item.get("grayPixels", 0) or 0) for item in row_items)),
                "messageKind": (
                    "media"
                    if any(str(item.get("messageKind", "text") or "text") == "media" for item in row_items)
                    else "text"
                ),
            }
        )
    collapsed.sort(key=lambda item: item["top"])
    return collapsed


def _message_kind_for_chat_text(text: str) -> str:
    return "media" if is_media_message(text) else "text"


def _resolve_text_bubble_role(
    *,
    bubble_role: str,
    left: float,
    width: float,
    green_pixels: int,
    gray_pixels: int,
    panel_kind: str,
) -> str:
    role = str(bubble_role or "unknown").strip().lower()
    right = float(left) + float(width)

    if panel_kind == "roster":
        force_outbound_left = 0.63
        force_outbound_right = 0.82
        force_inbound_left = 0.50
        force_inbound_right = 0.72
        fallback_outbound_left = 0.58
        fallback_outbound_right = 0.78
        fallback_inbound_right = 0.70
    else:
        force_outbound_left = 0.57
        force_outbound_right = 0.78
        force_inbound_left = 0.43
        force_inbound_right = 0.68
        fallback_outbound_left = 0.52
        fallback_outbound_right = 0.75
        fallback_inbound_right = 0.66

    if role == "inbound" and (left >= force_outbound_left or right >= force_outbound_right):
        return "unknown"
    elif role == "outbound" and left <= force_inbound_left and right <= force_inbound_right:
        return "unknown"

    if role in {"outbound", "inbound"}:
        return role

    if (
        green_pixels >= 32
        and green_pixels >= max(gray_pixels + 6, int(gray_pixels * 1.15))
        and (left >= fallback_outbound_left or right >= fallback_outbound_right)
    ):
        return "outbound"
    if (
        gray_pixels >= 48
        and gray_pixels >= max(green_pixels + 10, int(green_pixels * 1.35))
        and right <= fallback_inbound_right
    ):
        return "inbound"
    return "unknown"


def _resolve_media_bubble_role(*, left: float, width: float, panel_kind: str) -> str:
    right = float(left) + float(width)
    center = float(left) + (float(width) / 2.0)
    if panel_kind == "roster":
        outbound_left = 0.64
        outbound_center = 0.72
        inbound_left = 0.56
        inbound_center = 0.61
    else:
        outbound_left = 0.58
        outbound_center = 0.68
        inbound_left = 0.48
        inbound_center = 0.54
    if left >= outbound_left or center >= outbound_center:
        return "outbound"
    if left <= inbound_left and center <= inbound_center and right <= 0.86:
        return "inbound"
    return "unknown"


def _resolve_chat_item_role(
    *,
    text: str,
    bubble_role: str,
    left: float,
    width: float,
    green_pixels: int,
    gray_pixels: int,
    panel_kind: str,
) -> str:
    message_kind = _message_kind_for_chat_text(text)
    if message_kind == "media":
        return _resolve_media_bubble_role(left=left, width=width, panel_kind=panel_kind)
    return _resolve_text_bubble_role(
        bubble_role=bubble_role,
        left=left,
        width=width,
        green_pixels=green_pixels,
        gray_pixels=gray_pixels,
        panel_kind=panel_kind,
    )


def _extract_chat_panel(obs_list: list[dict[str, Any]], selected_title: str = "") -> dict[str, Any]:
    inbound_raw: list[dict[str, Any]] = []
    outbound_raw: list[dict[str, Any]] = []
    misc_raw: list[dict[str, Any]] = []
    ignored = {normalize_text(item) for item in IGNORE_TEXTS}
    for obs in sorted(obs_list, key=lambda item: item["bbox"]["top"]):
        text = obs["text"]
        left = obs["bbox"]["left"]
        top = obs["bbox"]["top"]
        width = obs["bbox"]["w"]
        if top < 0.11 or top > 0.90:
            continue
        if left < 0.38 or width < 0.02:
            continue
        if _is_time(text) or text == selected_title:
            continue
        if normalize_text(text) in ignored or text.startswith("Hold to Fn"):
            continue
        if not has_signal_text(text):
            continue
        bucket = {
            "text": text,
            "top": round(top, 4),
            "left": round(left, 4),
            "width": round(width, 4),
            "confidence": round(float(obs.get("confidence", 0.0) or 0.0), 4),
            "bubbleRole": str(obs.get("bubbleRole", "unknown")),
            "greenPixels": int(obs.get("greenPixels", 0) or 0),
            "grayPixels": int(obs.get("grayPixels", 0) or 0),
            "messageKind": _message_kind_for_chat_text(text),
        }
        bubble_role = _resolve_chat_item_role(
            text=text,
            bubble_role=str(obs.get("bubbleRole", "unknown")),
            left=float(left),
            width=float(width),
            green_pixels=int(obs.get("greenPixels", 0) or 0),
            gray_pixels=int(obs.get("grayPixels", 0) or 0),
            panel_kind="roster",
        )
        if bubble_role == "outbound":
            outbound_raw.append(bucket)
        elif bubble_role == "inbound":
            inbound_raw.append(bucket)
        else:
            misc_raw.append(bucket)
    inbound = _collapse_panel_lines(inbound_raw)
    outbound = _collapse_panel_lines(outbound_raw)
    misc = _collapse_panel_lines(misc_raw)
    return {
        "title": selected_title,
        "latestInbound": inbound[-1]["text"] if inbound else "",
        "latestOutbound": outbound[-1]["text"] if outbound else "",
        "inbound": inbound,
        "outbound": outbound,
        "misc": misc,
    }


def _extract_chat_window_panel(obs_list: list[dict[str, Any]], selected_title: str = "") -> dict[str, Any]:
    inbound_raw: list[dict[str, Any]] = []
    outbound_raw: list[dict[str, Any]] = []
    misc_raw: list[dict[str, Any]] = []
    ignored = {normalize_text(item) for item in IGNORE_TEXTS}
    for obs in sorted(obs_list, key=lambda item: item["bbox"]["top"]):
        text = obs["text"]
        left = obs["bbox"]["left"]
        top = obs["bbox"]["top"]
        width = obs["bbox"]["w"]
        if top < 0.12 or top > 0.92 or width < 0.025:
            continue
        if _is_time(text) or text == selected_title:
            continue
        if normalize_text(text) in ignored or text.startswith("Hold to Fn"):
            continue
        if top > 0.82 and left < 0.40 and width < 0.15:
            continue
        if not has_signal_text(text):
            continue
        bucket = {
            "text": text,
            "top": round(top, 4),
            "left": round(left, 4),
            "width": round(width, 4),
            "confidence": round(float(obs.get("confidence", 0.0) or 0.0), 4),
            "bubbleRole": str(obs.get("bubbleRole", "unknown")),
            "greenPixels": int(obs.get("greenPixels", 0) or 0),
            "grayPixels": int(obs.get("grayPixels", 0) or 0),
            "messageKind": _message_kind_for_chat_text(text),
        }
        bubble_role = _resolve_chat_item_role(
            text=text,
            bubble_role=str(obs.get("bubbleRole", "unknown")),
            left=float(left),
            width=float(width),
            green_pixels=int(obs.get("greenPixels", 0) or 0),
            gray_pixels=int(obs.get("grayPixels", 0) or 0),
            panel_kind="chat",
        )
        if bubble_role == "outbound":
            outbound_raw.append(bucket)
        elif bubble_role == "inbound":
            inbound_raw.append(bucket)
        else:
            misc_raw.append(bucket)
    inbound = _collapse_panel_lines(inbound_raw)
    outbound = _collapse_panel_lines(outbound_raw)
    misc = _collapse_panel_lines(misc_raw)
    return {
        "title": selected_title,
        "latestInbound": inbound[-1]["text"] if inbound else "",
        "latestOutbound": outbound[-1]["text"] if outbound else "",
        "inbound": inbound,
        "outbound": outbound,
        "misc": misc,
    }


def _panel_has_content(panel: dict[str, Any]) -> bool:
    return bool(
        panel.get("title")
        or panel.get("latestInbound")
        or panel.get("latestOutbound")
        or panel.get("inbound")
        or panel.get("outbound")
        or panel.get("misc")
    )


def _panel_vote_settings() -> tuple[int, float]:
    try:
        config = load_config()
    except Exception:
        return (3, 0.12)
    frame_count = max(1, min(3, int(config.get("chat_panel_vote_frames", 3) or 3)))
    interval = max(0.0, min(0.4, float(config.get("chat_panel_vote_interval_seconds", 0.12) or 0.12)))
    return frame_count, interval


def _panel_frame_score(panel: dict[str, Any]) -> float:
    inbound = len(list(panel.get("inbound") or []))
    outbound = len(list(panel.get("outbound") or []))
    misc = len(list(panel.get("misc") or []))
    return float(inbound * 2 + outbound * 2 + misc)


def _vote_panel_text(values: list[str]) -> str:
    cleaned = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not cleaned:
        return ""
    buckets: dict[str, list[str]] = {}
    for value in cleaned:
        buckets.setdefault(normalize_text(value), []).append(value)
    winner_key, winner_values = max(
        buckets.items(),
        key=lambda item: (len(item[1]), max(len(v) for v in item[1]), item[1][-1]),
    )
    del winner_key
    return max(winner_values, key=len)


def _vote_panel_side_items(
    frames: list[dict[str, Any]],
    side: str,
    anchor_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not frames:
        return list(anchor_items or [])
    counts: Counter[tuple[str, int, int, str]] = Counter()
    exemplars: dict[tuple[str, int, int, str], dict[str, Any]] = {}
    for frame in frames:
        items = list((frame.get("panel") or {}).get(side) or [])
        for item in items:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            signature = (
                normalize_text(text),
                int(round(float(item.get("top", 0.0) or 0.0) / 0.03)),
                int(round(float(item.get("left", 0.0) or 0.0) / 0.05)),
                str(item.get("messageKind", "text") or "text"),
            )
            counts[signature] += 1
            existing = exemplars.get(signature)
            current_conf = float(item.get("confidence", 0.0) or 0.0)
            if existing is None or current_conf > float(existing.get("confidence", 0.0) or 0.0) or (
                current_conf >= float(existing.get("confidence", 0.0) or 0.0) - 0.02
                and len(text) > len(str(existing.get("text") or ""))
            ):
                exemplars[signature] = dict(item)
    if not counts:
        return list(anchor_items or [])
    min_votes = 2 if len(frames) >= 2 else 1
    kept = [
        dict(exemplars[key])
        for key, count in counts.items()
        if count >= min_votes and key in exemplars
    ]
    if not kept:
        return list(anchor_items or [])
    kept.sort(key=lambda item: float(item.get("top", 0.0) or 0.0))
    return kept


def _vote_panel_frames(frames: list[dict[str, Any]], fallback_title: str = "") -> dict[str, Any]:
    if not frames:
        return {"panel": {}, "title": fallback_title, "path": ""}
    voted_title = _vote_panel_text([str(frame.get("title") or "") for frame in frames]) or str(fallback_title or "")
    def _frame_rank(index_frame: tuple[int, dict[str, Any]]) -> tuple[float, int]:
        index, frame = index_frame
        panel = frame.get("panel") or {}
        score = _panel_frame_score(panel)
        if normalize_text(str(frame.get("title") or "")) == normalize_text(voted_title):
            score += 1.5
        if str(panel.get("latestInbound") or "").strip():
            score += 0.5
        if str(panel.get("latestOutbound") or "").strip():
            score += 0.5
        return (score, -index)
    anchor = max(enumerate(frames), key=_frame_rank)[1]
    anchor_panel = dict(anchor.get("panel") or {})
    inbound = _vote_panel_side_items(frames, "inbound", list(anchor_panel.get("inbound") or []))
    outbound = _vote_panel_side_items(frames, "outbound", list(anchor_panel.get("outbound") or []))
    misc = _vote_panel_side_items(frames, "misc", list(anchor_panel.get("misc") or []))
    panel = {
        "title": voted_title or str(anchor_panel.get("title") or ""),
        "latestInbound": inbound[-1].get("text", "") if inbound else "",
        "latestOutbound": outbound[-1].get("text", "") if outbound else "",
        "inbound": inbound,
        "outbound": outbound,
        "misc": misc,
    }
    return {
        "panel": panel,
        "title": panel["title"],
        "path": str(anchor.get("path") or ""),
        "frameCount": len(frames),
    }


def probe(
    select_chat: str | None = None,
    sleep_after_click: float = 1.0,
    select_chat_click: dict[str, Any] | None = None,
    badge_rescue_targets: list[str] | None = None,
) -> dict[str, Any]:
    activate_wechat()
    roster_info = ensure_main_roster_window()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    roster_path = CAPTURE_DIR / f"wechat-roster-{timestamp}.png"
    capture_window(roster_path, roster_info)
    roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster")
    chats = annotate_unread_chats(
        extract_visible_chats(roster_obs, roster_info),
        roster_path,
        fallback_target_names=badge_rescue_targets,
    )
    preselected_title = _pick_selected_title(roster_obs)

    selected_requested = select_chat or ""
    selected_chat = None
    if select_chat:
        # Re-clicking the already-active roster row can intermittently open
        # WeChat's contact context menu instead of simply reselecting the chat.
        # If the right-panel title already matches the requested contact, trust
        # the current selection and skip the extra row click.
        if preselected_title and names_match(preselected_title, select_chat):
            selected_chat = preselected_title
        else:
            match = find_chat(chats, select_chat)
            click_hint = select_chat_click if isinstance(select_chat_click, dict) else {}
            click_x = int(click_hint.get("x", 0) or 0)
            click_y = int(click_hint.get("y", 0) or 0)
            use_click_hint = click_x > 0 and click_y > 0
            # Always prefer a fresh click target from the current roster snapshot.
            # Stale coordinates from an older probe can drift into the search-row
            # action area (such as the "+" quick-actions button) after window
            # focus/resize/list movement.
            if not match and not use_click_hint:
                return {
                    "status": "chat_not_visible",
                    "target": select_chat,
                    "window": roster_info,
                    "screenshot": str(roster_path),
                    "visibleChats": chats,
                }
            current_window_id = int(roster_info.get("window_id", 0) or 0)
            if match:
                click_coords(
                    match["click"]["x"],
                    match["click"]["y"],
                    window_id=current_window_id,
                    auto_focus=False,
                )
                selected_chat = match["name"]
            else:
                click_coords(
                    click_x,
                    click_y,
                    window_id=current_window_id,
                    auto_focus=False,
                )
                selected_chat = select_chat
            time.sleep(max(sleep_after_click, 0.2))
            roster_info = ensure_main_roster_window()
            capture_window(roster_path, roster_info)
            roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster")
            chats = annotate_unread_chats(
                extract_visible_chats(roster_obs, roster_info),
                roster_path,
                fallback_target_names=badge_rescue_targets,
            )

    windows = list_wechat_windows()
    chat_window = choose_chat_window(windows)
    fallback_title = selected_chat or _pick_selected_title(roster_obs)
    panel_vote_frames, panel_vote_interval = _panel_vote_settings()
    if chat_window:
        chat_frames: list[dict[str, Any]] = []
        for index in range(panel_vote_frames):
            chat_path = CAPTURE_DIR / f"wechat-chat-{timestamp}-{index}.png"
            capture_window(chat_path, chat_window)
            chat_obs = _prepare_ocr_items(chat_path)
            title = _pick_chat_window_title(chat_obs)
            panel = _extract_chat_window_panel(chat_obs, selected_title=title)
            chat_frames.append({"panel": panel, "title": title, "path": str(chat_path)})
            if index + 1 < panel_vote_frames and panel_vote_interval > 0:
                time.sleep(panel_vote_interval)
        voted = _vote_panel_frames(chat_frames, fallback_title=fallback_title)
        voted_path = str(voted.get("path") or chat_frames[-1].get("path") or "").strip()
        chat_path = Path(voted_path) if voted_path else None
        panel = voted.get("panel") or {}
        if not _panel_has_content(panel):
            title = fallback_title
            roster_frames: list[dict[str, Any]] = [
                {
                    "panel": _extract_chat_panel(roster_obs, selected_title=title),
                    "title": title,
                    "path": str(roster_path),
                }
            ]
            for index in range(1, panel_vote_frames):
                capture_window(roster_path, roster_info)
                roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster")
                frame_title = _pick_selected_title(roster_obs) or title
                roster_frames.append(
                    {
                        "panel": _extract_chat_panel(roster_obs, selected_title=frame_title),
                        "title": frame_title,
                        "path": str(roster_path),
                    }
                )
                if index + 1 < panel_vote_frames and panel_vote_interval > 0:
                    time.sleep(panel_vote_interval)
            panel = (_vote_panel_frames(roster_frames, fallback_title=title).get("panel") or {})
            chat_path = roster_path
            # Chat subwindow capture is empty or stale; force downstream typing to use
            # the main roster window input coordinates.
            chat_window = None
    else:
        roster_frames = [
            {
                "panel": _extract_chat_panel(roster_obs, selected_title=fallback_title),
                "title": fallback_title,
                "path": str(roster_path),
            }
        ]
        for index in range(1, panel_vote_frames):
            capture_window(roster_path, roster_info)
            roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster")
            frame_title = _pick_selected_title(roster_obs) or fallback_title
            roster_frames.append(
                {
                    "panel": _extract_chat_panel(roster_obs, selected_title=frame_title),
                    "title": frame_title,
                    "path": str(roster_path),
                }
            )
            if index + 1 < panel_vote_frames and panel_vote_interval > 0:
                time.sleep(panel_vote_interval)
        voted = _vote_panel_frames(roster_frames, fallback_title=fallback_title)
        chat_path = None
        panel = voted.get("panel") or {}

    active_chat = (
        _sanitize_chat_title_text((chat_window or {}).get("title"))
        or _sanitize_chat_title_text(panel.get("title"))
        or ""
    )
    return {
        "status": "ok",
        "window": roster_info,
        "chatWindow": chat_window,
        "screenshot": str(chat_path or roster_path),
        "screenshots": {"roster": str(roster_path), "chat": str(chat_path) if chat_path else ""},
        "visibleChats": chats,
        "selectedChat": selected_chat,
        "selectedChatRequested": selected_requested,
        "activeChat": active_chat,
        "selectionConfirmed": True if not selected_requested else names_match(selected_requested, active_chat),
        "chatPanel": panel,
    }


def _input_coords(probe_result: dict[str, Any]) -> tuple[int, int]:
    window = probe_result.get("chatWindow") or probe_result.get("window") or {}
    is_split_chat = bool(probe_result.get("chatWindow"))
    if is_split_chat:
        x = window["x"] + int(window["width"] * 0.28)
        y = window["y"] + int(window["height"] * 0.92)
    else:
        x = window["x"] + int(window["width"] * 0.63)
        y = window["y"] + int(window["height"] * 0.91)
    return x, y


def focus_input_box(probe_result: dict[str, Any]) -> None:
    x, y = _input_coords(probe_result)
    click_coords(x, y)
    time.sleep(0.15)
    click_coords(x, y)
    time.sleep(0.2)


def read_input_box_text(probe_result: dict[str, Any]) -> str:
    previous_clipboard = _read_clipboard_text()
    # Put a sentinel into clipboard first. If Cmd+C fails or focus misses
    # the input box, clipboard stays sentinel and we treat it as "no input"
    # instead of misclassifying stale clipboard text as manual edits.
    sentinel = f"__WECHAT_INPUT_PROBE_{time.time_ns()}__"
    focus_input_box(probe_result)
    try:
        _write_clipboard_text(sentinel)
        time.sleep(0.05)
        run(
            ["osascript", "-e", 'tell application "System Events" to keystroke "a" using {command down}'],
            timeout=30,
        )
        time.sleep(0.1)
        run(
            ["osascript", "-e", 'tell application "System Events" to keystroke "c" using {command down}'],
            timeout=30,
        )
        time.sleep(0.12)
        copied = _read_clipboard_text().strip()
        if not copied or copied == sentinel:
            return ""
        return copied
    finally:
        _write_clipboard_text(previous_clipboard)


def paste_text(text: str) -> None:
    run_peekaboo_variants(
        peekaboo_commands(["paste", "--text", text, "--app", WECHAT_APP, "--json"]),
        timeout=60,
    )
    time.sleep(0.2)


def send_message() -> None:
    run_peekaboo_variants(
        peekaboo_commands(["press", "return", "--app", WECHAT_APP, "--json"]),
        timeout=60,
    )
    time.sleep(0.5)
