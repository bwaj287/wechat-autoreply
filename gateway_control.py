#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

from wechat_autoreply.config_store import load_config, save_config, set_enabled, status_line
from wechat_autoreply.paths import EVENTS_PATH
from wechat_autoreply.state_store import load_state


TRACE_TYPES = {
    "draft_saved_locally",
    "pending_refreshed_latest",
    "auto_sent",
    "pending_cancelled",
    "runner_error",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Control the WeChat auto-reply runner.")
    parser.add_argument("command", choices=["on", "off", "status"])
    return parser.parse_args()


def _shorten(text: str, limit: int = 48) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _format_ts(raw: str) -> str:
    try:
        return datetime.fromisoformat(raw).strftime("%H:%M:%S")
    except Exception:
        return raw


def _format_trace(event: dict[str, Any]) -> str:
    when = _format_ts(str(event.get("ts") or ""))
    event_type = str(event.get("type") or "")
    contact = str(event.get("contact") or "").strip()
    prefix = f"[{when}]"
    if contact:
        prefix = f"{prefix} {contact}"
    if event_type == "draft_saved_locally":
        draft = _shorten(str(event.get("draft_text") or ""))
        return f"{prefix} 草稿已生成：{draft}"
    if event_type == "pending_refreshed_latest":
        draft = _shorten(str(event.get("draft_text") or ""))
        return f"{prefix} 草稿已更新：{draft}"
    if event_type == "auto_sent":
        draft = _shorten(str(event.get("draft_text") or ""))
        return f"{prefix} 已发送：{draft}"
    if event_type == "pending_cancelled":
        reason = str(event.get("reason") or "unknown")
        return f"{prefix} 已取消：{reason}"
    if event_type == "runner_error":
        reason = _shorten(str(event.get("error") or "unknown error"))
        return f"{prefix} 运行报错：{reason}"
    return ""


def recent_trace_lines(limit: int = 5) -> list[str]:
    if not EVENTS_PATH.exists():
        return []
    lines: list[str] = []
    raw_lines = EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    for raw in reversed(raw_lines):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if str(event.get("type") or "") not in TRACE_TYPES:
            continue
        formatted = _format_trace(event)
        if not formatted:
            continue
        lines.append(formatted)
        if len(lines) >= limit:
            break
    lines.reverse()
    return lines


def status_output(config: dict[str, Any], state: dict[str, Any]) -> str:
    pending_queue = state.get("pending_queue") or []
    pending_count = len(pending_queue) if isinstance(pending_queue, list) else (1 if state.get("pending") else 0)
    base = status_line(config, pending_count)
    traces = recent_trace_lines()
    if not traces:
        return base
    return "\n".join([base, "最近记录：", *traces])


def main() -> int:
    args = parse_args()
    if args.command == "on":
        config = set_enabled(True)
    elif args.command == "off":
        config = set_enabled(False)
    else:
        config = load_config()
        save_config(config)

    state = load_state()
    if args.command == "status":
        print(status_output(config, state))
    else:
        pending_queue = state.get("pending_queue") or []
        pending_count = len(pending_queue) if isinstance(pending_queue, list) else (1 if state.get("pending") else 0)
        print(status_line(config, pending_count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
