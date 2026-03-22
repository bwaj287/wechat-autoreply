#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime
from typing import Any

from wechat_autoreply.config_store import load_config, save_config, set_enabled, status_line
from wechat_autoreply.paths import EVENTS_PATH
from wechat_autoreply.state_store import default_state, load_state, save_state, utc_now_iso


TRACE_TYPES = {
    "draft_saved_locally",
    "pending_refreshed_latest",
    "auto_sent",
    "pending_cancelled",
    "runner_error",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Control the WeChat auto-reply runner.")
    parser.add_argument("command", choices=["on", "off", "status", "queue", "reset", "restart"])
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
    pending_queue = _pending_queue(state)
    pending_count = len(pending_queue)
    base = status_line(config, pending_count)
    traces = recent_trace_lines()
    if not traces:
        return base
    return "\n".join([base, "最近记录：", *traces])


def _pending_queue(state: dict[str, Any]) -> list[dict[str, Any]]:
    pending_queue = state.get("pending_queue")
    if isinstance(pending_queue, list):
        return [item for item in pending_queue if isinstance(item, dict)]
    pending = state.get("pending")
    return [pending] if isinstance(pending, dict) else []


def _format_queue_item(index: int, item: dict[str, Any], now: float) -> str:
    contact = str(item.get("contact") or "?").strip()
    inbound = _shorten(str(item.get("inbound_text") or ""), 36) or "-"
    draft = _shorten(str(item.get("draft_text") or ""), 42) or "-"
    due_at = float(item.get("due_at", 0.0) or 0.0)
    due_seconds = int(round(due_at - now)) if due_at else 0
    due_label = f"{max(due_seconds, 0)}s后" if due_seconds >= 0 else f"已超时{abs(due_seconds)}s"
    return f"{index}. {contact} · {due_label} | 入站：{inbound} | 草稿：{draft}"


def queue_output(config: dict[str, Any], state: dict[str, Any]) -> str:
    queue = _pending_queue(state)
    base = status_line(config, len(queue))
    if not queue:
        return f"{base}\n队列为空"
    now = time.time()
    lines = [base, "待发送队列："]
    for idx, item in enumerate(queue, 1):
        lines.append(_format_queue_item(idx, item, now))
    return "\n".join(lines)


def reset_runtime_state() -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_config()
    config["enabled"] = True
    save_config(config)

    state = default_state()
    state["last_run_at"] = utc_now_iso()
    save_state(state)

    subprocess.run(
        ["pkill", "-f", "/Users/shawnwang/Documents/Playground/main.py"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return config, state


def main() -> int:
    args = parse_args()
    if args.command == "on":
        config = set_enabled(True)
    elif args.command == "off":
        config = set_enabled(False)
    elif args.command in {"reset", "restart"}:
        config, state = reset_runtime_state()
    else:
        config = load_config()
        save_config(config)
        state = load_state()
    if args.command == "status":
        print(status_output(config, state))
    elif args.command == "queue":
        print(queue_output(config, state))
    elif args.command in {"reset", "restart"}:
        print("微信自动回复：已重置并重启（待发送 0）")
    else:
        pending_count = len(_pending_queue(state))
        print(status_line(config, pending_count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
