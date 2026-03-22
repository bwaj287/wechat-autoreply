#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime
from typing import Any

from wechat_autoreply.config_store import load_config, save_config, set_enabled, status_line
from wechat_autoreply.paths import EVENTS_PATH, PROJECT_ROOT
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
    parser.add_argument("command", choices=["on", "off", "status", "queue", "diagnose", "reset", "restart"])
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


def _recent_events(limit: int = 20) -> list[dict[str, Any]]:
    if not EVENTS_PATH.exists():
        return []
    events: list[dict[str, Any]] = []
    for raw in reversed(EVENTS_PATH.read_text(encoding="utf-8").splitlines()):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        if len(events) >= limit:
            break
    events.reverse()
    return events


def _format_epoch(raw: Any) -> str:
    try:
        value = float(raw or 0.0)
    except Exception:
        return "-"
    if value <= 0:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _format_event_compact(event: dict[str, Any]) -> str:
    ts = _format_ts(str(event.get("ts") or ""))
    event_type = str(event.get("type") or "")
    contact = str(event.get("contact") or "").strip()
    reason = str(event.get("reason") or "").strip()
    display_type = "非正常状态栏数字和红点" if event_type == "claim_logic_bug" else event_type
    if event_type == "claim_logic_bug":
        reason = "非正常状态栏数字和红点"
    parts = [f"[{ts}]", display_type]
    if contact:
        parts.append(f"contact={contact}")
    if reason:
        parts.append(f"reason={reason}")
    if event_type == "menu_bar_checked":
        parts.append(f"signal={event.get('signal', '')}")
    if event_type == "pending_gc_removed":
        parts.append(f"removed={event.get('removed_count', 0)}")
    return " ".join(parts)


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


def diagnose_output(config: dict[str, Any], state: dict[str, Any]) -> str:
    queue = _pending_queue(state)
    lines: list[str] = [status_line(config, len(queue)), "诊断信息："]
    lines.append(f"- enabled: {bool(config.get('enabled'))}")
    lines.append(f"- queue_length: {len(queue)}")
    lines.append(f"- last_run_at: {state.get('last_run_at') or '-'}")
    lines.append(f"- last_error: {state.get('last_error') or '-'}")
    lines.append(f"- last_menu_signal: {state.get('last_menu_signal') or '-'}")
    lines.append(f"- last_menu_unread: {bool(state.get('last_menu_unread'))}")
    lines.append(f"- last_menu_check_at: {_format_epoch(state.get('last_menu_check_at'))}")
    lines.append(f"- last_roster_sweep_at: {_format_epoch(state.get('last_roster_sweep_at'))}")
    lines.append(
        f"- stale_pending_ttl_seconds: {int(float(config.get('pending_stale_ttl_seconds', 86400) or 86400))}"
    )
    if queue:
        lines.append("待发送队列：")
        now = time.time()
        for idx, item in enumerate(queue, 1):
            lines.append(_format_queue_item(idx, item, now))
    else:
        lines.append("待发送队列：空")

    recent = _recent_events(limit=20)
    if recent:
        lines.append("最近事件(20)：")
        for event in recent:
            lines.append(_format_event_compact(event))
    return "\n".join(lines)


def reset_runtime_state() -> tuple[dict[str, Any], dict[str, Any]]:
    def stop_runner_processes() -> None:
        for target in (PROJECT_ROOT / "main.py", PROJECT_ROOT / "apps" / "runner" / "cli.py"):
            subprocess.run(
                ["pkill", "-f", str(target)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    config = load_config()
    # Freeze runner first to avoid queue/state races during reset.
    config["enabled"] = False
    save_config(config)

    stop_runner_processes()

    state = default_state()
    state["last_run_at"] = utc_now_iso()
    save_state(state)

    # One more stop in case launchd auto-respawned during reset.
    stop_runner_processes()
    config["enabled"] = True
    save_config(config)
    state = load_state()
    return config, state


def main() -> int:
    args = parse_args()
    if args.command == "on":
        config = set_enabled(True)
        state = load_state()
    elif args.command == "off":
        config = set_enabled(False)
        state = load_state()
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
    elif args.command == "diagnose":
        print(diagnose_output(config, state))
    elif args.command in {"reset", "restart"}:
        print("微信自动回复：已重置并重启（待发送 0）")
    else:
        pending_count = len(_pending_queue(state))
        print(status_line(config, pending_count))
    return 0
