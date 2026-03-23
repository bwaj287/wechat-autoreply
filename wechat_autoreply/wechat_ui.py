from __future__ import annotations

import json
import os
import re
import tempfile
import subprocess
import time
from pathlib import Path
from typing import Any

from .ocr import ocr_image
from .paths import BUBBLE_ROLE_HELPER, CAPTURE_DIR, ROW_BADGE_HELPER, WECHAT_APP
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


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def normalize_name_for_match(text: str) -> str:
    value = normalize_text(text)
    return value.translate(str.maketrans({"0": "o"}))


def names_match(a: str, b: str) -> bool:
    an = normalize_name_for_match(a)
    bn = normalize_name_for_match(b)
    return bool(an and bn and (an == bn or an in bn or bn in an))


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


def has_meaningful_text(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    if SHORT_PING_RE.fullmatch(value):
        return True
    return has_text_signal(value) or is_nontext_message(value)


def has_signal_text(text: str) -> bool:
    return has_meaningful_text(text)


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
tell application "System Events"
  keystroke "h" using {command down}
end tell
return "hidden"
""",
    ]
    errors: list[str] = []
    for script in scripts:
        try:
            result = run(["osascript", "-e", script], timeout=30).stdout.strip().lower()
            if result in {"hidden", "missing", ""}:
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


def _prepare_ocr_items(image_path: Path) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for item in ocr_image(image_path):
        text = (item.get("text") or "").strip()
        if not text:
            continue
        bbox = item.get("bbox") or {}
        x = float(bbox.get("x", 0.0))
        y = float(bbox.get("y", 0.0))
        w = float(bbox.get("w", 0.0))
        h = float(bbox.get("h", 0.0))
        top = 1.0 - (y + h)
        prepared.append(
            {
                "text": text,
                "confidence": float(item.get("confidence", 0.0)),
                "bbox": {"x": x, "y": y, "w": w, "h": h, "top": top, "left": x},
                "center": {"x": x + w / 2.0, "y": top + h / 2.0},
            }
        )
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


def annotate_unread_chats(chats: list[dict[str, Any]], roster_path: Path) -> list[dict[str, Any]]:
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
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        json.dump(rows, handle, ensure_ascii=False)
        tmp_path = Path(handle.name)
    try:
        payload = json.loads(
            run(["swift", str(ROW_BADGE_HELPER), str(roster_path), str(tmp_path)], timeout=120).stdout
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    marks = {item["index"]: item for item in payload.get("rows", [])}
    for index, chat in enumerate(chats):
        mark = marks.get(index, {})
        chat["redPixelCount"] = int(mark.get("redPixelCount", 0))
        chat["unread"] = bool(mark.get("unread", False))
    return chats


def find_chat(chats: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    for chat in chats:
        if names_match(chat["name"], target):
            return chat
    return None


def click_coords(x: int, y: int) -> None:
    run_peekaboo_variants(
        peekaboo_commands(["click", "--coords", f"{x},{y}", "--app", WECHAT_APP, "--json"]),
        timeout=60,
    )


def _pick_selected_title(obs_list: list[dict[str, Any]]) -> str:
    candidates: list[dict[str, Any]] = []
    ignored = {normalize_text(item) for item in IGNORE_TEXTS}
    for obs in obs_list:
        text = obs["text"]
        left = obs["bbox"]["left"]
        top = obs["bbox"]["top"]
        width = obs["bbox"]["w"]
        if left < 0.38 or top > 0.12 or width < 0.04:
            continue
        if _is_time(text):
            continue
        if normalize_text(text) in ignored:
            continue
        candidates.append(obs)
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item["bbox"]["top"], -item["bbox"]["w"]))
    return candidates[0]["text"]


def _pick_chat_window_title(obs_list: list[dict[str, Any]]) -> str:
    candidates: list[dict[str, Any]] = []
    ignored = {normalize_text(item) for item in IGNORE_TEXTS}
    for obs in obs_list:
        text = obs["text"]
        left = obs["bbox"]["left"]
        top = obs["bbox"]["top"]
        width = obs["bbox"]["w"]
        if top > 0.15 or left > 0.30 or width < 0.04:
            continue
        if _is_time(text):
            continue
        if normalize_text(text) in ignored:
            continue
        candidates.append(obs)
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item["bbox"]["top"], item["bbox"]["left"]))
    return candidates[0]["text"]


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
            }
        )
    collapsed.sort(key=lambda item: item["top"])
    return collapsed


def _resolve_chat_bubble_role(
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

    # Right-side bubbles are outbound even when color sampling is noisy in dark mode.
    if panel_kind == "roster":
        force_outbound_left = 0.63
        force_outbound_right = 0.82
        force_inbound_left = 0.50
        force_inbound_right = 0.72
        fallback_outbound_left = 0.58
        fallback_outbound_right = 0.79
        fallback_inbound_left = 0.30
    else:
        force_outbound_left = 0.57
        force_outbound_right = 0.78
        force_inbound_left = 0.43
        force_inbound_right = 0.68
        fallback_outbound_left = 0.52
        fallback_outbound_right = 0.76
        fallback_inbound_left = 0.08

    if role == "inbound" and (left >= force_outbound_left or right >= force_outbound_right):
        role = "outbound"
    elif role == "outbound" and left <= force_inbound_left and right <= force_inbound_right:
        role = "inbound"

    if role not in {"outbound", "inbound"}:
        if green_pixels >= 28 and green_pixels >= gray_pixels:
            return "outbound"
        if gray_pixels >= 44 and gray_pixels > int(green_pixels * 1.2):
            return "inbound"
        if left >= fallback_outbound_left or right >= fallback_outbound_right:
            return "outbound"
        if left >= fallback_inbound_left:
            return "inbound"
        return "unknown"

    return role


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
        }
        bubble_role = _resolve_chat_bubble_role(
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
        }
        bubble_role = _resolve_chat_bubble_role(
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


def probe(select_chat: str | None = None, sleep_after_click: float = 1.0) -> dict[str, Any]:
    activate_wechat()
    windows = list_wechat_windows()
    roster_info = choose_roster_window(windows)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    roster_path = CAPTURE_DIR / f"wechat-roster-{timestamp}.png"
    capture_window(roster_path, roster_info)
    roster_obs = _prepare_ocr_items(roster_path)
    chats = annotate_unread_chats(extract_visible_chats(roster_obs, roster_info), roster_path)

    selected_requested = select_chat or ""
    selected_chat = None
    if select_chat:
        match = find_chat(chats, select_chat)
        if not match:
            return {
                "status": "chat_not_visible",
                "target": select_chat,
                "window": roster_info,
                "screenshot": str(roster_path),
                "visibleChats": chats,
            }
        click_coords(match["click"]["x"], match["click"]["y"])
        time.sleep(max(sleep_after_click, 0.2))
        windows = list_wechat_windows()
        roster_info = choose_roster_window(windows)
        capture_window(roster_path, roster_info)
        roster_obs = _prepare_ocr_items(roster_path)
        chats = annotate_unread_chats(extract_visible_chats(roster_obs, roster_info), roster_path)
        selected_chat = match["name"]

    windows = list_wechat_windows()
    chat_window = choose_chat_window(windows)
    fallback_title = selected_chat or _pick_selected_title(roster_obs)
    if chat_window:
        chat_path = CAPTURE_DIR / f"wechat-chat-{timestamp}.png"
        capture_window(chat_path, chat_window)
        chat_obs = _prepare_ocr_items(chat_path)
        title = _pick_chat_window_title(chat_obs)
        panel = _extract_chat_window_panel(chat_obs, selected_title=title)
        if not _panel_has_content(panel):
            title = fallback_title
            panel = _extract_chat_panel(roster_obs, selected_title=title)
            chat_path = roster_path
            # Chat subwindow capture is empty or stale; force downstream typing to use
            # the main roster window input coordinates.
            chat_window = None
    else:
        chat_path = None
        panel = _extract_chat_panel(roster_obs, selected_title=fallback_title)

    active_chat = (chat_window or {}).get("title") or panel.get("title") or ""
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
