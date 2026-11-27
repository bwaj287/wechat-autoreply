from __future__ import annotations

import json
import os
import re
import tempfile
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from Foundation import NSAppleEventDescriptor, NSAppleEventSendNeverInteract, NSAppleEventSendNoReply
from PIL import Image, ImageOps
from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionAll, kCGWindowListOptionOnScreenOnly

from .badge_detection import fallback_row_badge_detection as _fallback_row_badge_detection
from .config_store import load_allowed_contacts, load_config, save_config
from .event_log import append_event
from .json_output import load_first_json_object
from .ocr import contact_label, ocr_image, ocr_region
from .paths import BUBBLE_ROLE_HELPER, CAPTURE_DIR, PEEKABOO, WECHAT_APP
from .peekaboo_utils import peekaboo_commands, run, run_peekaboo_variants

IGNORE_TEXTS = {
    "Search",
    "Hold to Fn to use voice input",
}
LOGIN_ENTRY_TEXTS = {"enter weixin", "enter wechat"}
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
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
WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"


class WeChatNotFrontmost(RuntimeError):
    pass


def _existing_windows(*, onscreen_only: bool = False, verified_pid: int | None = None) -> list[dict[str, Any]]:
    if verified_pid is None:
        verified_pid = _running_wechat_pid()
        if not verified_pid:
            return []
    option = kCGWindowListOptionOnScreenOnly if onscreen_only else kCGWindowListOptionAll
    windows = []
    for entry in CGWindowListCopyWindowInfo(option, kCGNullWindowID) or []:
        pid = int(entry.get("kCGWindowOwnerPID", 0))
        if pid != verified_pid or entry.get("kCGWindowLayer") != 0:
            continue
        bounds = entry.get("kCGWindowBounds") or {}
        width, height = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
        if min(width, height) < 50 or not entry.get("kCGWindowNumber"):
            continue
        windows.append({
            "window_id": int(entry["kCGWindowNumber"]), "pid": pid,
            "title": str(entry.get("kCGWindowName") or ""),
            "x": int(bounds.get("X", 0)), "y": int(bounds.get("Y", 0)), "width": width, "height": height,
        })
    return windows


def _require_front_window(window_id: int | None = None) -> dict[str, Any]:
    # Query System Events rather than addressing WeChat, which could launch a stopped app.
    script = f'''
tell application "System Events"
  set p to first application process whose frontmost is true
  if (bundle identifier of p) is not "{WECHAT_BUNDLE_ID}" or not (visible of p) then return ""
  set w to value of attribute "AXFocusedWindow" of p
  if (value of attribute "AXMinimized" of w) is not false then return ""
  if (subrole of w) is not "AXStandardWindow" or (exists sheet 1 of w) then return ""
  return (unix id of p as text) & linefeed & (name of w as text)
end tell
'''
    try:
        answer = run(["osascript", "-e", script], timeout=3).stdout.strip().split("\n", 1)
        if len(answer) != 2 or not answer[0].isdigit() or not answer[1]:
            raise WeChatNotFrontmost("WeChat has no focused, visible standard window")
        pid = int(answer[0])
        windows = _existing_windows(onscreen_only=True, verified_pid=pid)
        if windows:
            front = windows[0]
            if (_frontmost_pid() == pid == front["pid"] and front["title"] == answer[1]
                    and (window_id is None or window_id == front["window_id"])):
                return front
    except WeChatNotFrontmost:
        raise
    except Exception as exc:
        raise WeChatNotFrontmost("Could not inspect the foreground WeChat window") from exc
    raise WeChatNotFrontmost("WeChat has no verified foreground window")


def is_wechat_frontmost() -> bool:
    try:
        _require_front_window()
        return True
    except WeChatNotFrontmost:
        return False


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def wechat_login_required(obs_list: list[dict[str, Any]]) -> bool:
    texts = {normalize_text(str(item.get("text") or "")) for item in obs_list}
    if texts & LOGIN_ENTRY_TEXTS:
        return True
    return "switch account" in texts and "transfer files only" in texts


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


def _is_service_chat_name(text: str) -> bool:
    name = normalize_text(text)
    stem = _canonical_name_key(text)
    return (
        name == "service"
        or name.startswith("service ")
        or (("..." in name or "…" in name) and len(stem) >= 4 and "service".startswith(stem))
    )


def _service_accounts_heading(obs_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            obs
            for obs in obs_list
            if "service accounts" in normalize_text(str(obs.get("text") or ""))
            and float(obs.get("bbox", {}).get("left", 1)) < 0.30
            and float(obs.get("bbox", {}).get("top", 1)) < 0.13
        ),
        None,
    )


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


def _running_app_handle(pid: int) -> str:
    # A fresh process reads immutable identity. AppKit's process list goes stale in the blocking runner.
    script = '''
ObjC.import("AppKit");
function run(args) {
    const app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(Number(args[0]));
    if (!app || app.isNil()) return "";
    const bundle = ObjC.unwrap(app.bundleIdentifier), date = app.launchDate;
    if (!bundle || !date || date.isNil()) return "";
    return [args[0], date.timeIntervalSince1970, bundle].join(":");
}
'''
    try:
        return run(["osascript", "-l", "JavaScript", "-e", script, str(pid)], timeout=3).stdout.strip()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise WeChatNotFrontmost("Could not read the foreground process identity") from exc


def _running_wechat_pid() -> int:
    script = f'tell application "System Events" to get unix id of every application process whose bundle identifier is "{WECHAT_BUNDLE_ID}"'
    try:
        value = run(["osascript", "-e", script], timeout=3).stdout.strip()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise WeChatNotFrontmost("Could not query running WeChat processes") from exc
    return int(value) if value.isdigit() else 0


def _frontmost_pid() -> int:
    # AppKit caches changing application state until the main run loop advances. The runner is a blocking CLI.
    script = 'tell application "System Events" to get unix id of first application process whose frontmost is true'
    try:
        return int(run(["osascript", "-e", script], timeout=3).stdout.strip())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise WeChatNotFrontmost("Could not read the current foreground owner") from exc


def activate_wechat(expected_front: str | None = None, *, ensure_idle: Callable[[], None] | None = None) -> None:
    pid = _running_wechat_pid()
    if not pid:
        raise WeChatNotFrontmost("Expected one running WeChat process")
    owner_pid = _frontmost_pid()
    owner = "" if owner_pid == pid else _running_app_handle(owner_pid)
    if expected_front is not None and (expected_front != owner or (not owner and owner_pid != pid)):
        raise WeChatNotFrontmost("Foreground owner changed before activating WeChat")

    # All commands address living processes. Activation happens at most once, even if focus never arrives.
    script = f'''
tell application "System Events"
  set candidates to every application process whose bundle identifier is "{WECHAT_BUNDLE_ID}"
  if (count candidates) is not 1 then return "target_changed"
  set p to item 1 of candidates
  if (unix id of p) is not {pid} then return "target_changed"
  set ownerPID to unix id of first application process whose frontmost is true
  if ownerPID is not {owner_pid} then return "owner_changed_before_unhide"
  if not (visible of p) then set visible of p to true
  set ownerPID to unix id of first application process whose frontmost is true
  if ownerPID is not {pid} then
    if ownerPID is not {owner_pid} then return "owner_changed_after_unhide"
    set frontmost of p to true
  end if
  repeat 10 times
    set ownerPID to unix id of first application process whose frontmost is true
    if ownerPID is {pid} then exit repeat
    if ownerPID is not {owner_pid} then return "owner_changed_during_activation"
    delay 0.05
  end repeat
  if not (frontmost of p) then return "foreground_request_unconfirmed"
  if not (exists window 1 of p) then return "no_windows"
  set mainWindows to {{}}
  repeat with w in windows of p
    if subrole of w is "AXStandardWindow" and (name of w is in {{"WeChat", "Weixin", "微信"}}) then set end of mainWindows to contents of w
  end repeat
  if (count mainWindows) is 1 then
    set mainWindow to item 1 of mainWindows
    if value of attribute "AXMinimized" of mainWindow is true then
      if not (frontmost of p) then return "owner_changed_before_unminimize"
      set value of attribute "AXMinimized" of mainWindow to false
    end if
  end if
  return true
end tell
'''
    try:
        result = run(["osascript", "-e", script], timeout=4).stdout.strip()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise WeChatNotFrontmost("Could not activate the existing WeChat window") from exc
    if result == "no_windows":
        if _existing_windows(onscreen_only=True, verified_pid=pid):
            return
        if ensure_idle is None:
            raise WeChatNotFrontmost("Cannot reopen a WeChat window without checking user activity")
        # The Dock's reopen event restores a closed main window. Address the existing PID only.
        target = NSAppleEventDescriptor.descriptorWithProcessIdentifier_(pid)
        event = NSAppleEventDescriptor.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_(
            int.from_bytes(b"aevt", "big"), int.from_bytes(b"rapp", "big"), target, -1, 0,
        )
        guard = f'''
tell application "System Events"
  set p to first application process whose frontmost is true
  if (unix id of p) is not {pid} or (bundle identifier of p) is not "{WECHAT_BUNDLE_ID}" or not (visible of p) then return "owner_changed"
  if exists window 1 of p then return "window_appeared"
  return "reopen"
end tell
'''
        try:
            result = run(["osascript", "-e", guard], timeout=3).stdout.strip()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise WeChatNotFrontmost("Could not verify windowless foreground WeChat") from exc
        if result == "window_appeared":
            return
        if result == "reopen":
            ensure_idle()
            _, error = event.sendEventWithOptions_timeout_error_(
                NSAppleEventSendNoReply | NSAppleEventSendNeverInteract, 1.0, None,
            )
            if error is not None:
                raise WeChatNotFrontmost(f"WeChat window reopen request failed ({error.code()})")
            return
    if result != "true":
        raise WeChatNotFrontmost(f"WeChat activation deferred: {result}")


def _escape_applescript_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def capture_frontmost_app() -> str:
    handle = _running_app_handle(_frontmost_pid())
    if not handle:
        raise WeChatNotFrontmost("Could not identify the original foreground process")
    return "" if handle.endswith(":" + WECHAT_BUNDLE_ID) else handle


def restore_frontmost_app(handle: str) -> None:
    pid, _, _ = handle.partition(":")
    if not pid.isdigit():
        return
    if handle.endswith(":" + WECHAT_BUNDLE_ID) or _running_app_handle(int(pid)) != handle:
        return
    current_pid = _frontmost_pid()
    if current_pid == int(pid):
        return
    script = f'''
tell application "System Events"
  if (unix id of first application process whose frontmost is true) is not {current_pid} then return false
  set p to first application process whose unix id is {int(pid)}
  if not (visible of p) then return false
  set frontmost of p to true
  return true
end tell
'''
    run(["osascript", "-e", script], timeout=3)


def hide_wechat() -> bool:
    script = f'''
tell application "System Events"
  set p to first application process whose frontmost is true
  if bundle identifier of p is not "{WECHAT_BUNDLE_ID}" then return "not_owner"
  set visible of p to false
  repeat 10 times
    if not (visible of p) then return "hidden"
    delay 0.05
  end repeat
  return "hide_unconfirmed"
end tell
'''
    result = run(["osascript", "-e", script], timeout=3).stdout.strip()
    if result == "not_owner":
        return False
    if result != "hidden":
        raise RuntimeError("Could not confirm that WeChat was hidden")
    return True


def _read_clipboard_text(*, strict: bool = False) -> str:
    try:
        return run(["pbpaste"], check=strict, timeout=5).stdout
    except Exception as exc:
        if strict:
            raise WeChatNotFrontmost("Could not verify the input clipboard") from exc
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


def list_wechat_windows(*, passive: bool = False) -> list[dict[str, Any]]:
    if passive:
        return _existing_windows()
    payload = load_first_json_object(
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
    candidates = [window for window in windows if window["title"] in {"Weixin", "WeChat", "微信"}]
    if not candidates:
        raise RuntimeError("no roster window found")
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


def choose_chat_window(windows: list[dict[str, Any]], *, requested_title: str = "") -> dict[str, Any] | None:
    usable = [
        window
        for window in windows
        if str(window.get("title") or "").strip()
        and window["title"] not in {"Weixin", "WeChat", "WeChat (Window)"}
        and (not requested_title or names_match(str(window["title"]), requested_title))
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


def capture_window(path: Path, info: dict[str, Any], *, passive: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if passive:
        window_id = int(info.get("window_id", 0))
        if window_id <= 0:
            raise RuntimeError("No existing WeChat window ID for capture")
        run(["/usr/sbin/screencapture", "-x", "-o", f"-l{window_id}", str(path)], timeout=10)
        return path
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


def probe_roster_background(*, include_panel: bool = False) -> dict[str, Any]:
    """Inspect roster badges without activating or focusing WeChat."""
    windows = list_wechat_windows(passive=True)
    roster_info = choose_roster_window(windows)
    timestamp = time.time_ns()
    roster_path = CAPTURE_DIR / f"wechat-roster-background-{timestamp}.png"
    capture_window(roster_path, roster_info, passive=True)
    roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster", recover_outbound=include_panel)
    if not roster_obs:
        return {"status": "window_unreadable", "window": roster_info, "screenshot": str(roster_path), "visibleChats": []}
    if wechat_login_required(roster_obs):
        return {
            "status": "login_required",
            "window": roster_info,
            "screenshot": str(roster_path),
            "visibleChats": [],
        }
    heading = _service_accounts_heading(roster_obs)
    if heading:
        bbox = heading["bbox"]
        return {
            "status": "service_accounts",
            "window": roster_info,
            "screenshot": str(roster_path),
            "visibleChats": [],
            "serviceBack": (roster_info["x"] + int(bbox["left"] * roster_info["width"]) + 6,
                            roster_info["y"] + int((bbox["top"] + bbox["h"] / 2) * roster_info["height"])),
        }
    chats = annotate_unread_chats(
        extract_visible_chats(roster_obs, roster_info),
        roster_path,
    )
    result = {
        "status": "ok",
        "window": roster_info,
        "screenshot": str(roster_path),
        "visibleChats": chats,
    }
    if include_panel:
        title = _pick_selected_title(roster_obs)
        result.update(activeChat=title, chatPanel=_extract_chat_panel(roster_obs, selected_title=title))
    return result


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


def _prepare_ocr_items(
    image_path: Path, *, panel_hint: str = "", recover_outbound: bool = False,
) -> list[dict[str, Any]]:
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

    def _record_item(text: str, x: float, y: float, w: float, h: float, confidence: float, candidates: list) -> None:
        top = 1.0 - (y + h)
        prepared.append(
            {
                "text": text,
                "confidence": confidence,
                "candidates": candidates,
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
            _record_item(text, x, y, w, h, confidence, list(item.get("candidates") or []))

        # Enhanced roster OCR pass:
        # only update existing boxes, never add brand-new boxes.
        # This avoids false rows introduced by aggressive enhancement.
        for source in source_images[1:]:
            try:
                enhanced_items = ocr_image(source)
            except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as exc:
                append_event("ocr_enhancement_failed", screenshot=str(image_path), error=str(exc))
                continue
            for item in enhanced_items:
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
                            "candidates": list(item.get("candidates") or []),
                            "bbox": {"x": x, "y": y, "w": w, "h": h, "top": top, "left": x},
                            "center": {"x": x + w / 2.0, "y": top + h / 2.0},
                        }
                    )
    finally:
        for path in temp_variants:
            path.unlink(missing_ok=True)
    base_count = len(prepared)
    if recover_outbound:
        # Whole-window OCR can omit green bubbles; never invent roster rows from this crop.
        left = 0.30 if panel_hint == "roster" else 0.0
        try:
            for item in ocr_region(image_path, (left, 0.50, 0.99, 0.90)):
                text = str(item.get("text") or "").strip()
                bbox = item["bbox"]
                x, y, w, h = (float(bbox[key]) for key in ("x", "y", "w", "h"))
                if text and not _find_bbox_match(x, 1.0 - y - h, w, h):
                    _record_item(text, x, y, w, h, float(item.get("confidence", 0)), list(item.get("candidates") or []))
        except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as exc:
            append_event("ocr_outbound_recovery_failed", screenshot=str(image_path), error=str(exc))
    _annotate_bubble_roles(prepared, image_path)
    prepared = prepared[:base_count] + [
        item for item in prepared[base_count:] if item.get("bubbleRole") == "outbound"
    ]
    contacts = load_allowed_contacts()
    for item in prepared:
        item["contactText"] = item["text"] if _is_service_chat_name(item["text"]) else contact_label(item, contacts)
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
        role_payload = load_first_json_object(proc.stdout)
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


def _timestamp_aligned_chats(obs_list: list[dict[str, Any]], window_info: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Locate roster cells from their right-aligned times and common name column."""
    time_label = re.compile(r"(?i)(?:(?:today|yesterday|今天|昨天)\s*)?\d{1,2}[:：]\d{2}(?:\s*[ap]m)?")
    stamps = [
        item for item in obs_list if 0.04 < item["bbox"]["top"] < 0.985
        and 0.06 < item["bbox"]["left"] < 0.45 and time_label.fullmatch(item["text"].strip())
    ]
    if len(stamps) < 3:
        return None

    def right(item):
        return item["bbox"]["left"] + item["bbox"]["w"]

    def cy(item):
        return item["bbox"]["top"] + item["bbox"]["h"] / 2

    # Separate the roster's timestamp column from any clocks in a chat bubble.
    header = _pick_selected_title(obs_list)
    header_lefts = [item["bbox"]["left"] for item in obs_list
                    if item["bbox"]["top"] <= .06 and .25 <= item["bbox"]["left"] <= .62
                    and header and _sanitize_chat_title_text(item.get("contactText", item["text"])) == header]
    roster_right = min(header_lefts, default=_roster_band_profiles(window_info)[-1][1])
    stamps = [stamp for stamp in stamps if right(stamp) < roster_right]
    if len(stamps) < 3:
        return None
    column = max(([s for s in stamps if abs(right(s) - right(anchor)) <= 0.009] for anchor in stamps), key=len)
    anchors = []
    for stamp in sorted(column, key=cy):
        if not anchors or cy(stamp) - cy(anchors[-1]) > 0.012:
            anchors.append(stamp)
    if len(anchors) < 3:
        return None
    gaps = [cy(b) - cy(a) for a, b in zip(anchors, anchors[1:])]
    pitch = min(gaps, key=lambda gap: (-sum(abs(gap - other) <= gap * .12 for other in gaps), gap))
    if not 0.025 <= pitch <= 0.20 or sum(abs(gap - pitch) <= pitch * .12 for gap in gaps) < 2:
        return None

    ignored = {normalize_text(item) for item in IGNORE_TEXTS}
    left_limit = _roster_band_profiles(window_info)[-1][0]
    items = [
        item for item in obs_list if left_limit < item["bbox"]["left"] < right(anchors[0])
        and 0.04 < item["bbox"]["top"] < 0.985 and normalize_text(item["text"]) not in ignored
        and not time_label.fullmatch(item["text"].strip())
    ]
    title_lefts = []
    for stamp in anchors:
        aligned = [item["bbox"]["left"] for item in items
                   if abs(cy(item) - cy(stamp)) < pitch * .15 and item["bbox"]["left"] < stamp["bbox"]["left"]]
        if aligned:
            title_lefts.append(min(aligned))
    if len(title_lefts) < 2:
        return None
    title_left = sorted(title_lefts)[len(title_lefts) // 2]
    labels = sorted((item for item in items if abs(item["bbox"]["left"] - title_left) <= 0.013), key=cy)
    rows = []
    last_title_y = -1.0
    for title in labels:
        y = cy(title)
        if y - last_title_y < pitch * .3:
            continue
        stamp = min(anchors, key=lambda item: abs(cy(item) - y))
        offset = round((y - cy(stamp)) / pitch)
        if abs(offset) > 1 or abs(y - cy(stamp) - offset * pitch) > pitch * .15:
            continue
        last_title_y = y
        if _is_service_chat_name(title["text"]):
            continue
        preview = next((item["text"] for item in labels if pitch * .18 < cy(item) - y < pitch * .60), "")
        gx, gy = _global_coords(window_info, title)
        rows.append({
            "name": title.get("contactText", title["text"]), "preview": preview,
            "time": stamp["text"] if offset == 0 else "",
            "ocrTop": round(title["bbox"]["top"], 4), "ocrLeft": round(title["bbox"]["left"], 4),
            "click": {"x": gx, "y": gy},
        })
    return rows


def extract_visible_chats(obs_list: list[dict[str, Any]], window_info: dict[str, Any]) -> list[dict[str, Any]]:
    aligned = _timestamp_aligned_chats(obs_list, window_info)
    if aligned is not None:
        return aligned
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
            if _is_service_chat_name(name_item["text"]):
                continue
            if len(name_item["text"]) <= 1 and not re.search(r"[A-Za-z\u4e00-\u9fff]", name_item["text"]):
                continue
            gx, gy = _global_coords(window_info, name_item)
            chats.append(
                {
                    "name": name_item.get("contactText", name_item["text"]),
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
        # The Service entry opens a non-chat panel and must never be selected.
        return [chat for chat in cleaned if not _is_service_chat_name(chat["name"])]

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
                "nameTop": float(chat.get("ocrTop", 0.0) or 0.0),
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


def _system_events(script: str) -> None:
    try:
        run(["osascript", "-e", script], check=False, timeout=10)
    except Exception:
        pass


def _press_escape() -> None:
    _system_events('tell application "System Events" to key code 53')


def _command_a() -> None:
    _system_events('tell application "System Events" to keystroke "a" using {command down}')


def _command_v() -> None:
    _system_events('tell application "System Events" to keystroke "v" using {command down}')


def _roster_search_coords(window_info: dict[str, Any]) -> tuple[int, int]:
    width = int(window_info.get("width", 0) or 0)
    height = int(window_info.get("height", 0) or 0)
    x_offset = min(220, max(135, int(width * 0.17)))
    y_offset = min(42, max(26, int(height * 0.04)))
    return int(window_info.get("x", 0) or 0) + x_offset, int(window_info.get("y", 0) or 0) + y_offset


def _perform_ui_action(
    ui_action: Callable[..., Any] | None,
    action: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    return ui_action(action, *args, **kwargs) if ui_action else action(*args, **kwargs)


def _select_chat_via_search(
    target: str,
    roster_info: dict[str, Any],
    roster_path: Path,
    *,
    sleep_after_click: float,
    badge_rescue_targets: list[str] | None,
    ui_action: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    previous_clipboard = _read_clipboard_text()
    try:
        current_window_id = int(roster_info.get("window_id", 0) or 0)
        search_x, search_y = _roster_search_coords(roster_info)
        _perform_ui_action(ui_action, click_coords, search_x, search_y, window_id=current_window_id, auto_focus=False)
        time.sleep(0.15)
        _perform_ui_action(ui_action, _command_a)
        time.sleep(0.05)
        _perform_ui_action(ui_action, _write_clipboard_text, target)
        time.sleep(0.05)
        _perform_ui_action(ui_action, _command_v)
        time.sleep(max(0.45, min(1.0, sleep_after_click)))

        roster_info = _perform_ui_action(ui_action, ensure_main_roster_window)
        capture_window(roster_path, roster_info)
        roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster")
        chats = annotate_unread_chats(
            extract_visible_chats(roster_obs, roster_info),
            roster_path,
            fallback_target_names=badge_rescue_targets,
        )
        match = find_chat(chats, target)
        if not match:
            _perform_ui_action(ui_action, _press_escape)
            return {
                "selectedChat": "",
                "window": roster_info,
                "rosterObs": roster_obs,
                "visibleChats": chats,
            }

        current_window_id = int(roster_info.get("window_id", 0) or 0)
        _perform_ui_action(
            ui_action,
            click_coords,
            match["click"]["x"],
            match["click"]["y"],
            window_id=current_window_id,
            auto_focus=False,
        )
        selected_chat = str(match.get("name") or target)
        time.sleep(0.2)
        _perform_ui_action(ui_action, _press_escape)
        time.sleep(max(sleep_after_click, 0.2))
        roster_info = _perform_ui_action(ui_action, ensure_main_roster_window)
        capture_window(roster_path, roster_info)
        roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster", recover_outbound=True)
        chats = annotate_unread_chats(
            extract_visible_chats(roster_obs, roster_info),
            roster_path,
            fallback_target_names=badge_rescue_targets,
        )
        return {
            "selectedChat": selected_chat,
            "window": roster_info,
            "rosterObs": roster_obs,
            "visibleChats": chats,
        }
    finally:
        if _read_clipboard_text() == target:
            _write_clipboard_text(previous_clipboard)


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
        if left < 0.25 or left > 0.62 or top > 0.06 or width < 0.02:
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
    return _sanitize_chat_title_text(candidates[0].get("contactText", candidates[0]["text"]))


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
    return _sanitize_chat_title_text(candidates[0].get("contactText", candidates[0]["text"]))


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
        if panel_kind != "roster":
            return "outbound"
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


def _extract_chat_panel(
    obs_list: list[dict[str, Any]],
    selected_title: str = "",
    *,
    standalone_window: bool = False,
) -> dict[str, Any]:
    inbound_raw: list[dict[str, Any]] = []
    outbound_raw: list[dict[str, Any]] = []
    misc_raw: list[dict[str, Any]] = []
    ignored = {normalize_text(item) for item in IGNORE_TEXTS}
    for obs in sorted(obs_list, key=lambda item: item["bbox"]["top"]):
        text = obs["text"]
        left = obs["bbox"]["left"]
        top = obs["bbox"]["top"]
        width = obs["bbox"]["w"]
        if standalone_window:
            if top < 0.12 or top > 0.92 or width < 0.025:
                continue
        elif top < 0.11 or top > 0.90 or left < 0.30 or width < 0.02:
            continue
        if _is_time(text) or text == selected_title:
            continue
        if normalize_text(text) in ignored or text.startswith("Hold to Fn"):
            continue
        if standalone_window and top > 0.82 and left < 0.40 and width < 0.15:
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


def _extract_chat_window_panel(obs_list: list[dict[str, Any]], selected_title: str = "") -> dict[str, Any]:
    return _extract_chat_panel(obs_list, selected_title, standalone_window=True)


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


def _panel_read_is_stable(frames: list[dict[str, Any]], requested_title: str) -> bool:
    if len(frames) != 2:
        return False
    first, second = frames
    first_title = str(first.get("title") or "").strip()
    second_title = str(second.get("title") or "").strip()
    title_key = normalize_name_for_match(first_title)
    if not title_key or title_key != normalize_name_for_match(second_title):
        return False
    if requested_title and title_key != normalize_name_for_match(requested_title):
        return False

    def messages(frame: dict[str, Any]) -> tuple[tuple[tuple[str, str], ...], ...]:
        panel = frame.get("panel") or {}
        return tuple(
            tuple(
                (normalize_text(str(item.get("text") or "")), str(item.get("messageKind") or "text"))
                for item in panel.get(side) or []
            )
            for side in ("inbound", "outbound", "misc")
        )

    first_messages = messages(first)
    return any(first_messages) and first_messages == messages(second)


def _strict_interaction(args: list[str], window_id: int | None, *, bounds: dict[str, Any] | None = None) -> None:
    front = _require_front_window(window_id)
    if bounds and any(front[key] != bounds[key] for key in ("x", "y", "width", "height")):
        raise WeChatNotFrontmost("WeChat window moved since the last capture")
    command = [*args, "--app", f"PID:{front['pid']}", "--window-id", str(front["window_id"]),
               "--no-auto-focus", "--json"]
    # Never replay a click or Return after a transport error or a focus change.
    try:
        run(peekaboo_commands(command)[0], timeout=15)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        _require_front_window(window_id)
        raise


def _verified_panel(contact: str, roster: dict[str, Any], window: dict[str, Any]) -> dict[str, Any]:
    detached = window["window_id"] != roster["window"]["window_id"]
    frames = []
    for index in range(2):
        if not detached and index == 0:
            title, panel, path = roster["activeChat"], roster["chatPanel"], roster["screenshot"]
        else:
            image = CAPTURE_DIR / f"wechat-panel-read-{time.time_ns()}.png"
            capture_window(image, window, passive=True)
            obs = _prepare_ocr_items(image, panel_hint="" if detached else "roster", recover_outbound=True)
            title = _pick_chat_window_title(obs) if detached else _pick_selected_title(obs)
            panel = _extract_chat_panel(obs, selected_title=title, standalone_window=detached)
            path = str(image)
        if normalize_text(title) != normalize_text(contact):
            return {"status": "selection_unconfirmed", "selectionConfirmed": False,
                    "selectionReason": "chat_header_mismatch" if title else "chat_header_unreadable"}
        frames.append({"title": title, "panel": panel, "path": path})
    if not _panel_read_is_stable(frames, contact):
        return {"status": "window_unreadable", "selectionConfirmed": False, "selectionReason": "panel_frames_disagree"}
    last = frames[-1]
    return {
        **roster, "chatWindow": window if detached else None,
        "screenshot": last["path"], "screenshots": {"roster": roster["screenshot"], "chat": last["path"]},
        "activeChat": last["title"], "selectedChat": last["title"], "selectedChatRequested": contact,
        "selectionConfirmed": True, "chatPanel": last["panel"],
    }


def _exact_row(roster: dict[str, Any], contact: str) -> dict[str, Any] | None:
    rows = [row for row in roster["visibleChats"] if normalize_text(row["name"]) == normalize_text(contact)]
    return rows[0] if len(rows) == 1 else None


def _search_field_focused(pid: int) -> bool:
    script = f'''
tell application "System Events"
  set p to first application process whose frontmost is true
  if unix id of p is not {int(pid)} then return false
  set field to value of attribute "AXFocusedUIElement" of p
  if subrole of field is "AXSearchField" then return true
  if role of field is not "AXTextField" then return false
  return (value of attribute "AXPlaceholderValue" of field) is in {{"Search", "搜索", "搜尋"}}
end tell
'''
    try:
        return run(["osascript", "-e", script], timeout=3).stdout.strip() == "true"
    except Exception:
        return False


def probe_chat_background(contact: str) -> dict[str, Any]:
    """Read an already-open target and corroborate its inbound bubble with the roster preview."""
    failed = {"status": "selection_unconfirmed", "selectionConfirmed": False}
    try:
        roster = probe_roster_background(include_panel=True)
        if roster["status"] != "ok":
            return {**roster, "selectionConfirmed": False}
        row = _exact_row(roster, contact)
        if not contact.strip() or not row or not row.get("numericBadge") or not row.get("preview"):
            return failed
        window = roster["window"]
        if normalize_text(roster["activeChat"]) != normalize_text(contact):
            matches = [w for w in list_wechat_windows(passive=True) if normalize_text(w["title"]) == normalize_text(contact)]
            if len(matches) != 1 or matches[0]["window_id"] == window["window_id"]:
                return failed
            window = matches[0]
        selected = _verified_panel(contact, roster, window)
        if not selected.get("selectionConfirmed"):
            return selected
        inbound = normalize_text(selected["chatPanel"].get("latestInbound", ""))
        preview = normalize_text(row["preview"])
        prefix = preview.removesuffix("…").removesuffix("...")
        if preview != inbound and not (prefix != preview and len(prefix) >= 3 and inbound.startswith(prefix)):
            return failed
        return selected
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError):
        return {"status": "window_unreadable", "selectionConfirmed": False}


def _strict_probe(contact: str, sleep_after_click: float, ui_action: Callable[..., Any] | None) -> dict[str, Any]:
    front = _require_front_window()
    window_id = front["window_id"]

    def interact(*args: str) -> None:
        _perform_ui_action(ui_action, _strict_interaction, list(args), window_id, bounds=front)

    def click(x: int, y: int) -> None:
        interact("click", "--coords", f"{x},{y}")

    roster = probe_roster_background(include_panel=True)
    on_roster = window_id == roster["window"]["window_id"]
    if on_roster and roster["status"] == "service_accounts":
        click(*roster["serviceBack"])
        time.sleep(0.3)
        roster = probe_roster_background(include_panel=True)
    if roster["status"] != "ok" or not contact:
        return roster
    if on_roster:
        if roster["window"]["window_id"] != window_id:
            return {"status": "selection_unconfirmed", "selectionConfirmed": False}
        if normalize_text(roster["activeChat"]) != normalize_text(contact):
            row = _exact_row(roster, contact)
            searching = row is None
            if searching:
                click(*_roster_search_coords(front))
                time.sleep(0.15)
                if not _search_field_focused(front["pid"]):
                    return {"status": "chat_not_visible", "selectionConfirmed": False, "selectionReason": "search_field_not_focused"}
                interact("hotkey", "--keys", "cmd,a")
                if not _search_field_focused(front["pid"]):
                    return {"status": "chat_not_visible", "selectionConfirmed": False, "selectionReason": "search_field_lost_focus"}
                interact("paste", "--text", contact)
                time.sleep(max(sleep_after_click, 0.45))
                matches = probe_roster_background()
                if matches["status"] == "ok" and matches["window"]["window_id"] == window_id:
                    row = _exact_row(matches, contact)
            if not row:
                interact("press", "escape")
                return {"status": "chat_not_visible", "selectionConfirmed": False, "selectionReason": "no_unique_search_result"}
            click(int(row["click"]["x"]), int(row["click"]["y"]))
            if searching:
                time.sleep(0.2)
                interact("press", "escape")
            time.sleep(max(sleep_after_click, 0.2))
            roster = probe_roster_background(include_panel=True)
            if roster["status"] != "ok" or roster["window"]["window_id"] != front["window_id"]:
                return {"status": "selection_unconfirmed", "selectionConfirmed": False}
    elif normalize_text(front["title"]) != normalize_text(contact):
        return {"status": "selection_unconfirmed", "selectionConfirmed": False, "selectionReason": "different_detached_chat"}
    selected = _verified_panel(contact, roster, front)
    _require_front_window(front["window_id"])
    return selected


def verify_chat_for_input(selected: dict[str, Any], contact: str) -> None:
    detached = bool(selected.get("chatWindow"))
    window = selected.get("chatWindow") or selected.get("window") or {}
    window_id = int(window.get("window_id") or 0)
    target = normalize_text(contact)
    if not window_id or not target or not selected.get("selectionConfirmed"):
        raise WeChatNotFrontmost("No verified selection for input")
    front = _require_front_window(window_id)
    if detached and normalize_text(front["title"]) != target:
        raise WeChatNotFrontmost("Foreground chat window changed")
    try:
        image = CAPTURE_DIR / f"wechat-input-check-{time.time_ns()}.png"
        capture_window(image, front, passive=True)
        obs = _prepare_ocr_items(image, panel_hint="" if detached else "roster")
        header = _pick_chat_window_title(obs) if detached else _pick_selected_title(obs)
        if normalize_text(header) != target:
            raise WeChatNotFrontmost("Foreground chat header changed")
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise WeChatNotFrontmost("Could not verify the current chat for input") from exc
    _require_front_window(window_id)


def probe(
    select_chat: str | None = None,
    sleep_after_click: float = 1.0,
    select_chat_click: dict[str, Any] | None = None,
    badge_rescue_targets: list[str] | None = None,
    ui_action: Callable[..., Any] | None = None,
    *, strict_foreground: bool = False,
) -> dict[str, Any]:
    if strict_foreground:
        return _strict_probe(select_chat or "", sleep_after_click, ui_action)
    _perform_ui_action(ui_action, activate_wechat)
    roster_info = _perform_ui_action(ui_action, ensure_main_roster_window)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    roster_path = CAPTURE_DIR / f"wechat-roster-{timestamp}.png"
    capture_window(roster_path, roster_info)
    roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster", recover_outbound=bool(select_chat))
    if not roster_obs:
        return {
            "status": "window_unreadable",
            "window": roster_info,
            "screenshot": str(roster_path),
            "visibleChats": [],
        }
    if wechat_login_required(roster_obs):
        return {
            "status": "login_required",
            "window": roster_info,
            "screenshot": str(roster_path),
            "screenshots": {"roster": str(roster_path), "chat": ""},
            "visibleChats": [],
            "selectedChat": "",
            "selectedChatRequested": select_chat or "",
            "activeChat": "",
            "selectionConfirmed": False,
            "chatPanel": {},
        }
    heading = _service_accounts_heading(roster_obs)
    if heading:
        bbox = heading["bbox"]
        back_x = roster_info["x"] + int(float(bbox["left"]) * roster_info["width"]) + 6
        back_y = roster_info["y"] + int((float(bbox["top"]) + float(bbox["h"]) / 2) * roster_info["height"])
        _perform_ui_action(
            ui_action,
            click_coords,
            back_x,
            back_y,
            window_id=int(roster_info.get("window_id", 0) or 0),
            auto_focus=False,
        )
        time.sleep(0.3)
        capture_window(roster_path, roster_info)
        roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster", recover_outbound=bool(select_chat))
        if _service_accounts_heading(roster_obs):
            return {
                "status": "service_accounts",
                "window": roster_info,
                "screenshot": str(roster_path),
                "visibleChats": [],
            }
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
            selected_via_search = False
            # Always prefer a fresh click target from the current roster snapshot.
            # Stale coordinates from an older probe can drift into the search-row
            # action area (such as the "+" quick-actions button) after window
            # focus/resize/list movement.
            if not match and not use_click_hint:
                search_result = _select_chat_via_search(
                    select_chat,
                    roster_info,
                    roster_path,
                    sleep_after_click=sleep_after_click,
                    badge_rescue_targets=badge_rescue_targets,
                    ui_action=ui_action,
                )
                selected_chat = str(search_result.get("selectedChat") or "")
                roster_info = dict(search_result.get("window") or roster_info)
                roster_obs = list(search_result.get("rosterObs") or roster_obs)
                chats = list(search_result.get("visibleChats") or chats)
                if not selected_chat:
                    return {
                        "status": "chat_not_visible",
                        "target": select_chat,
                        "window": roster_info,
                        "screenshot": str(roster_path),
                        "visibleChats": chats,
                    }
                match = find_chat(chats, select_chat)
                use_click_hint = False
                selected_via_search = True
            current_window_id = int(roster_info.get("window_id", 0) or 0)
            if not selected_via_search:
                if match:
                    _perform_ui_action(
                        ui_action,
                        click_coords,
                        match["click"]["x"],
                        match["click"]["y"],
                        window_id=current_window_id,
                        auto_focus=False,
                    )
                    selected_chat = match["name"]
                else:
                    _perform_ui_action(
                        ui_action,
                        click_coords,
                        click_x,
                        click_y,
                        window_id=current_window_id,
                        auto_focus=False,
                    )
                    selected_chat = select_chat
                time.sleep(max(sleep_after_click, 0.2))
                roster_info = _perform_ui_action(ui_action, ensure_main_roster_window)
                capture_window(roster_path, roster_info)
                roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster", recover_outbound=bool(select_chat))
                chats = annotate_unread_chats(
                    extract_visible_chats(roster_obs, roster_info),
                    roster_path,
                    fallback_target_names=badge_rescue_targets,
                )

    windows = list_wechat_windows()
    chat_window = choose_chat_window(windows, requested_title=selected_requested or preselected_title)
    fallback_title = selected_chat or _pick_selected_title(roster_obs)
    panel_vote_frames, panel_vote_interval = _panel_vote_settings()
    if chat_window:
        chat_frames: list[dict[str, Any]] = []
        for index in range(panel_vote_frames):
            chat_path = CAPTURE_DIR / f"wechat-chat-{timestamp}-{index}.png"
            capture_window(chat_path, chat_window)
            chat_obs = _prepare_ocr_items(chat_path, recover_outbound=True)
            title = _pick_chat_window_title(chat_obs)
            panel = _extract_chat_window_panel(chat_obs, selected_title=title)
            chat_frames.append({"panel": panel, "title": title, "path": str(chat_path)})
            if _panel_read_is_stable(chat_frames, selected_requested):
                break
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
                roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster", recover_outbound=bool(select_chat))
                frame_title = _pick_selected_title(roster_obs) or title
                roster_frames.append(
                    {
                        "panel": _extract_chat_panel(roster_obs, selected_title=frame_title),
                        "title": frame_title,
                        "path": str(roster_path),
                    }
                )
                if _panel_read_is_stable(roster_frames, selected_requested):
                    break
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
            roster_obs = _prepare_ocr_items(roster_path, panel_hint="roster", recover_outbound=bool(select_chat))
            frame_title = _pick_selected_title(roster_obs) or fallback_title
            roster_frames.append(
                {
                    "panel": _extract_chat_panel(roster_obs, selected_title=frame_title),
                    "title": frame_title,
                    "path": str(roster_path),
                }
            )
            if _panel_read_is_stable(roster_frames, selected_requested):
                break
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


def focus_input_box(
    probe_result: dict[str, Any],
    ui_action: Callable[..., Any] | None = None,
    *, strict_foreground: bool = False,
) -> None:
    x, y = _input_coords(probe_result)
    if strict_foreground:
        window = probe_result.get("chatWindow") or probe_result.get("window") or {}
        window_id = int(window.get("window_id") or 0)
        if window_id <= 0 or not probe_result.get("selectionConfirmed"):
            raise WeChatNotFrontmost("No verified chat window for input")
        _perform_ui_action(ui_action, _strict_interaction, ["click", "--coords", f"{x},{y}"], window_id, bounds=window)
        return
    _perform_ui_action(ui_action, click_coords, x, y)
    time.sleep(0.15)
    _perform_ui_action(ui_action, click_coords, x, y)
    time.sleep(0.2)


def read_input_box_text(
    probe_result: dict[str, Any],
    ui_action: Callable[..., Any] | None = None,
    *, strict_foreground: bool = False,
) -> str:
    previous_clipboard = _read_clipboard_text(strict=strict_foreground)
    # Put a sentinel into clipboard first. If Cmd+C fails or focus misses
    # the input box, clipboard stays sentinel and we treat it as "no input"
    # instead of misclassifying stale clipboard text as manual edits.
    sentinel = f"__WECHAT_INPUT_PROBE_{time.time_ns()}__"
    copied = sentinel
    focus_input_box(probe_result, ui_action=ui_action, strict_foreground=strict_foreground)
    try:
        _write_clipboard_text(sentinel)
        time.sleep(0.05)
        if strict_foreground and _read_clipboard_text(strict=True) != sentinel:
            raise WeChatNotFrontmost("Could not verify the input clipboard sentinel")
        for key in ("a", "c"):
            if strict_foreground:
                window = probe_result.get("chatWindow") or probe_result["window"]
                _perform_ui_action(ui_action, _strict_interaction, ["hotkey", "--keys", f"cmd,{key}"], window["window_id"])
            else:
                _perform_ui_action(ui_action, run, ["osascript", "-e",
                    f'tell application "System Events" to keystroke "{key}" using {{command down}}'], timeout=30)
            if key == "a":
                time.sleep(0.1)
        time.sleep(0.12)
        copied = _read_clipboard_text(strict=strict_foreground).strip()
        if not copied or copied == sentinel:
            return ""
        return copied
    finally:
        if _read_clipboard_text() in {sentinel, copied}:
            _write_clipboard_text(previous_clipboard)


def paste_text(text: str, *, strict_foreground: bool = False, window_id: int | None = None) -> None:
    if strict_foreground:
        if not window_id or window_id <= 0:
            raise WeChatNotFrontmost("No verified chat window for paste")
        _strict_interaction(["paste", "--text", text], window_id)
        time.sleep(0.2)
        return
    run_peekaboo_variants(
        peekaboo_commands(["paste", "--text", text, "--app", WECHAT_APP, "--json"]),
        timeout=60,
        retry_on_error=False,
    )
    time.sleep(0.2)


def send_message(*, strict_foreground: bool = False, window_id: int | None = None) -> None:
    if strict_foreground:
        if not window_id or window_id <= 0:
            raise WeChatNotFrontmost("No verified chat window for Return")
        _strict_interaction(["press", "return"], window_id)
        time.sleep(0.5)
        return
    # A transport error can arrive after Return landed; confirmation owns any retry.
    run_peekaboo_variants(
        peekaboo_commands(["press", "return", "--app", WECHAT_APP, "--json"]),
        timeout=60,
        retry_on_error=False,
    )
    time.sleep(0.5)
