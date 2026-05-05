#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from wechat_autoreply.contact_memory import (
    clear_contact_recent_memory,
    get_contact_memory,
    set_contact_profile,
    set_contact_profile_lock,
)
from wechat_autoreply.config_store import load_config, save_config, set_enabled, status_line
from wechat_autoreply.event_log import append_event
from wechat_autoreply.paths import EVENTS_PATH, PROJECT_ROOT
from wechat_autoreply.state_store import default_state, load_state, save_state, utc_now_iso


TRACE_TYPES = {
    "draft_saved_locally",
    "pending_refreshed_latest",
    "pending_message_changed_recheck",
    "auto_sent",
    "pending_cancelled",
    "runner_error",
    "claim_opened_no_new_message",
}

SUPPORTED_COMMANDS = [
    "on",
    "off",
    "status",
    "runner",
    "runner-start",
    "queue",
    "since",
    "sent-since",
    "diagnose",
    "reset",
    "restart",
    "style-show",
    "style-set",
    "memory-show",
    "memory-set",
    "memory-clear",
    "memory-lock",
    "memory-unlock",
    "command",
    "/command",
    "help",
    "/help",
]

RUNNER_PATTERNS = (
    str(PROJECT_ROOT / "main.py"),
    str(PROJECT_ROOT / "apps" / "runner" / "cli.py"),
)

HOST_PATTERNS = (
    "wechat-autoreply-v1-terminal-host.sh",
    "ensure-wechat-autoreply-v1-host.sh",
)

LAUNCH_AGENT_LABEL = "ai.openclaw.wechat.autoreply.v1"
LAUNCH_AGENT_PLIST = Path("/Users/shawnwang/Library/LaunchAgents/ai.openclaw.wechat.autoreply.v1.plist")
HOST_ENSURE_SCRIPT = Path("/Users/shawnwang/.openclaw/workspace/scripts/ensure-wechat-autoreply-v1-host.sh")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Control the WeChat auto-reply runner.")
    parser.add_argument(
        "command",
        choices=SUPPORTED_COMMANDS,
    )
    parser.add_argument("command_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    raw_args = list(args.command_args or [])
    style_like = {"style-set"}
    contact_arg_only = {"memory-show", "memory-clear", "memory-lock", "memory-unlock"}
    profile_arg_commands = {"memory-set"}
    if args.command in style_like:
        if not " ".join(raw_args).strip():
            parser.error('style-set requires style text, e.g. style-set "Natural, short, no period"')
    elif args.command in contact_arg_only:
        if len(raw_args) != 1 or not str(raw_args[0]).strip():
            parser.error(f"{args.command} requires exactly one contact name")
    elif args.command in profile_arg_commands:
        if len(raw_args) < 2 or not str(raw_args[0]).strip() or not " ".join(raw_args[1:]).strip():
            parser.error('memory-set requires contact name and profile text, e.g. memory-set May "Friends, casual, can tease"')
    elif raw_args:
        parser.error("this command does not accept extra arguments")
    args.style_text = raw_args
    args.contact_name = str(raw_args[0]).strip() if raw_args else ""
    args.profile_text = " ".join(raw_args[1:]).strip() if len(raw_args) >= 2 else ""
    if args.command == "style-set" and not " ".join(args.style_text).strip():
        parser.error('style-set requires style text, e.g. style-set "Natural, short, no period"')
    return args


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


def _parse_iso_datetime(raw: Any) -> datetime | None:
    try:
        value = str(raw or "").strip()
        if not value:
            return None
        return datetime.fromisoformat(value)
    except Exception:
        return None


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
    if event_type == "pending_message_changed_recheck":
        delay_seconds = int(float(event.get("delay_seconds", 0) or 0))
        delay_label = f"{delay_seconds}秒"
        if delay_seconds % 60 == 0 and delay_seconds > 0:
            delay_label = f"{delay_seconds // 60}分钟"
        truncation_like = bool(event.get("truncation_like"))
        suffix = "（疑似截断/OCR抖动）" if truncation_like else ""
        return f"{prefix} 发送前检测消息变化，延后{delay_label}复检{suffix}"
    if event_type == "auto_sent":
        draft = _shorten(str(event.get("draft_text") or ""))
        return f"{prefix} 已发送：{draft}"
    if event_type == "pending_cancelled":
        reason = str(event.get("reason") or "unknown")
        return f"{prefix} 已取消：{reason}"
    if event_type == "runner_error":
        reason = _shorten(str(event.get("error") or "unknown error"))
        return f"{prefix} 运行报错：{reason}"
    if event_type == "claim_opened_no_new_message":
        signal = str(event.get("signal") or "")
        non_whitelist_cleared = bool(event.get("non_whitelist_cleared"))
        rows = list(event.get("visible_unread_rows") or [])
        row_preview = _shorten(" | ".join(str(item) for item in rows), 60) if rows else "-"
        if non_whitelist_cleared:
            return f"{prefix} 打开微信后无白名单新消息（已清非白名单红点，signal={signal or '-'}, rows={row_preview}）"
        return f"{prefix} 打开微信后无可认领新消息（signal={signal or '-'}, rows={row_preview}）"
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


def _recent_open_without_new_message_lines(limit: int = 3) -> list[str]:
    if not EVENTS_PATH.exists():
        return []
    lines: list[str] = []
    raw_lines = EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    for raw in reversed(raw_lines):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if str(event.get("type") or "") != "claim_opened_no_new_message":
            continue
        formatted = _format_trace(event)
        if not formatted:
            continue
        lines.append(formatted)
        if len(lines) >= limit:
            break
    lines.reverse()
    return lines


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


def _pgrep_lines(pattern: str) -> list[str]:
    result = subprocess.run(["pgrep", "-fl", pattern], check=False, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        return []
    lines: list[str] = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "pgrep -fl" in line:
            continue
        lines.append(line)
    return lines


def _collect_process_lines(patterns: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    lines: list[str] = []
    for pattern in patterns:
        for line in _pgrep_lines(pattern):
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
    return lines


def _extract_pids(lines: list[str]) -> list[str]:
    pids: list[str] = []
    for line in lines:
        pid = line.split(" ", 1)[0].strip()
        if pid.isdigit():
            pids.append(pid)
    return pids


def _launch_agent_loaded() -> bool:
    if not LAUNCH_AGENT_PLIST.exists():
        return False
    uid = str(os.getuid())
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{LAUNCH_AGENT_LABEL}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _runner_snapshot() -> dict[str, Any]:
    runner_lines = _collect_process_lines(RUNNER_PATTERNS)
    host_lines = _collect_process_lines(HOST_PATTERNS)
    return {
        "runner_online": bool(runner_lines),
        "runner_pids": _extract_pids(runner_lines),
        "host_online": bool(host_lines),
        "host_pids": _extract_pids(host_lines),
        "launch_agent_loaded": _launch_agent_loaded(),
    }


def _runner_brief(snapshot: dict[str, Any]) -> str:
    if snapshot["runner_online"]:
        pids = ",".join(snapshot["runner_pids"]) if snapshot["runner_pids"] else "?"
        return f"Runner：在线（pid {pids}）"
    if snapshot["host_online"]:
        pids = ",".join(snapshot["host_pids"]) if snapshot["host_pids"] else "?"
        return f"Runner：离线（Host 在线 pid {pids}）"
    return "Runner：离线"


def ensure_runner_started() -> tuple[dict[str, Any], list[str]]:
    actions: list[str] = []
    uid = str(os.getuid())
    launch_target = f"gui/{uid}/{LAUNCH_AGENT_LABEL}"
    if LAUNCH_AGENT_PLIST.exists():
        boot = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCH_AGENT_PLIST)],
            check=False,
            capture_output=True,
            text=True,
        )
        actions.append("launchctl_bootstrap_ok" if boot.returncode == 0 else f"launchctl_bootstrap_rc{boot.returncode}")
        kick = subprocess.run(
            ["launchctl", "kickstart", "-k", launch_target],
            check=False,
            capture_output=True,
            text=True,
        )
        actions.append("launchctl_kickstart_ok" if kick.returncode == 0 else f"launchctl_kickstart_rc{kick.returncode}")
    else:
        actions.append("launch_plist_missing")

    interim = _runner_snapshot()
    if not interim["runner_online"]:
        if HOST_ENSURE_SCRIPT.exists():
            host = subprocess.run(
                ["/bin/zsh", str(HOST_ENSURE_SCRIPT)],
                check=False,
                capture_output=True,
                text=True,
            )
            actions.append("host_ensure_ok" if host.returncode == 0 else f"host_ensure_rc{host.returncode}")
        else:
            actions.append("host_ensure_missing")

    time.sleep(1.0)
    return _runner_snapshot(), actions


def status_output(config: dict[str, Any], state: dict[str, Any]) -> str:
    pending_queue = _pending_queue(state)
    pending_count = len(pending_queue)
    base = status_line(config, pending_count)
    traces = recent_trace_lines()
    no_new_lines = _recent_open_without_new_message_lines(limit=3)
    runner = _runner_snapshot()
    lines = [base, _runner_brief(runner)]
    if traces:
        lines.extend(["最近记录：", *traces])
    if no_new_lines:
        lines.extend(["最近空开窗记录：", *no_new_lines])
    return "\n".join(lines)


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
    runner = _runner_snapshot()
    lines: list[str] = [status_line(config, len(queue)), "诊断信息："]
    lines.append(f"- enabled: {bool(config.get('enabled'))}")
    lines.append(f"- runner_online: {runner['runner_online']}")
    lines.append(f"- runner_pids: {','.join(runner['runner_pids']) if runner['runner_pids'] else '-'}")
    lines.append(f"- host_online: {runner['host_online']}")
    lines.append(f"- host_pids: {','.join(runner['host_pids']) if runner['host_pids'] else '-'}")
    lines.append(f"- launch_agent_loaded: {runner['launch_agent_loaded']}")
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


def sent_since_output(config: dict[str, Any], state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    queue = _pending_queue(state)
    now_iso = utc_now_iso()
    now_dt = _parse_iso_datetime(now_iso)
    baseline_iso = str(state.get("last_since_check_at") or state.get("last_sent_since_check_at") or "").strip()
    baseline_dt = _parse_iso_datetime(baseline_iso)

    if not baseline_dt or not now_dt:
        state["last_since_check_at"] = now_iso
        save_state(state)
        lines = [
            status_line(config, len(queue)),
            f"since 基线已创建：{_format_epoch(now_dt.timestamp() if now_dt else 0)}",
            "从现在开始统计，下次执行 since 会告诉你这段时间自动发了多少条",
        ]
        return "\n".join(lines), state

    auto_sent_events: list[dict[str, Any]] = []
    if EVENTS_PATH.exists():
        for raw in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(event.get("type") or "") != "auto_sent":
                continue
            event_dt = _parse_iso_datetime(event.get("ts"))
            if not event_dt or event_dt <= baseline_dt or event_dt > now_dt:
                continue
            auto_sent_events.append(event)

    state["last_since_check_at"] = now_iso
    save_state(state)

    lines = [
        status_line(config, len(queue)),
        f"自上次 since 查询以来自动发送 {len(auto_sent_events)} 条",
        f"统计区间：{_format_epoch(baseline_dt.timestamp())} -> {_format_epoch(now_dt.timestamp())}",
    ]
    if auto_sent_events:
        lines.append("发送记录：")
        for index, event in enumerate(auto_sent_events, 1):
            contact = str(event.get("contact") or "?").strip()
            draft = _shorten(str(event.get("draft_text") or ""), 42) or "-"
            lines.append(f"{index}. [{_format_ts(str(event.get('ts') or ''))}] {contact}：{draft}")
    return "\n".join(lines), state


def style_show_output(config: dict[str, Any]) -> str:
    style = str(config.get("reply_style_instructions") or "").strip()
    if not style:
        style = "（空）"
    return "\n".join(["微信自动回复语气规则：", style])


def memory_show_output(config: dict[str, Any], state: dict[str, Any], contact: str) -> str:
    memory = get_contact_memory(contact)
    queue = _pending_queue(state)
    profile = str(memory.get("profile") or "").strip() or "（空）"
    recent_summary = str(memory.get("recent_summary") or "").strip() or "（空）"
    locked = "是" if bool(memory.get("profile_locked")) else "否"
    updated_at = str(memory.get("updated_at") or "").strip() or "-"
    profile_updated_at = str(memory.get("profile_updated_at") or "").strip() or "-"
    recent_events = list(memory.get("recent_events") or [])
    lines = [status_line(config, len(queue)), f"联系人记忆：{memory.get('contact') or contact}"]
    lines.append(f"- 长期画像已锁定：{locked}")
    lines.append(f"- 长期画像：{profile}")
    lines.append(f"- 短期摘要：{recent_summary}")
    lines.append(f"- recent_events：{len(recent_events)}")
    lines.append(f"- updated_at：{updated_at}")
    lines.append(f"- profile_updated_at：{profile_updated_at}")
    return "\n".join(lines)


def runner_output(config: dict[str, Any], state: dict[str, Any]) -> str:
    queue = _pending_queue(state)
    snapshot = _runner_snapshot()
    lines = [status_line(config, len(queue)), "Runner 状态："]
    lines.append(f"- runner_online: {snapshot['runner_online']}")
    lines.append(f"- runner_pids: {','.join(snapshot['runner_pids']) if snapshot['runner_pids'] else '-'}")
    lines.append(f"- host_online: {snapshot['host_online']}")
    lines.append(f"- host_pids: {','.join(snapshot['host_pids']) if snapshot['host_pids'] else '-'}")
    lines.append(f"- launch_agent_loaded: {snapshot['launch_agent_loaded']}")
    lines.append(f"- enabled_switch: {bool(config.get('enabled'))}")
    lines.append(f"- last_run_at: {state.get('last_run_at') or '-'}")
    if not snapshot["runner_online"]:
        lines.append("建议：执行 on 或 restart 以拉起 runner")
    return "\n".join(lines)


def runner_start_output(
    config: dict[str, Any],
    state: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    actions: list[str],
) -> str:
    queue = _pending_queue(state)
    lines = [status_line(config, len(queue)), "Runner 拉起结果："]
    if before["runner_online"]:
        lines.append("- before: already_online")
        lines.append("- action: skipped")
    else:
        lines.append("- before: offline")
        lines.append(f"- action: {', '.join(actions) if actions else 'none'}")
    lines.append(f"- runner_online: {after['runner_online']}")
    lines.append(f"- runner_pids: {','.join(after['runner_pids']) if after['runner_pids'] else '-'}")
    lines.append(f"- host_online: {after['host_online']}")
    lines.append(f"- launch_agent_loaded: {after['launch_agent_loaded']}")
    if not after["runner_online"]:
        lines.append("建议：执行 restart；若仍失败，检查 launchd/Terminal 权限")
    return "\n".join(lines)


def help_output(config: dict[str, Any], state: dict[str, Any]) -> str:
    lines = [
        status_line(config, len(_pending_queue(state))),
        "Gateway 指令帮助：",
        "",
        "基础控制：",
        "- on：开启自动回复",
        "- off：关闭自动回复",
        "- status：查看开关状态 + 最近关键记录",
        "- runner：查看 runner/host 进程状态",
        "- runner-start：拉起 runner（离线时）",
        "- queue：查看待发送队列",
        "- since：查看自上次 since 查询以来自动发送了多少条",
        "- diagnose：查看详细诊断与最近事件",
        "- reset：清空 runtime state 并重启（等价 restart）",
        "- restart：清空 runtime state 并重启",
        "",
        "语气设置：",
        "- style-show：查看当前语气规则",
        '- style-set "<规则>"：更新语气规则',
        "",
        "联系人记忆：",
        "- memory-show <联系人>：查看该联系人的长期画像 + 短期摘要",
        '- memory-set <联系人> "<画像>"：手动设置该联系人的长期画像',
        "- memory-clear <联系人>：清空该联系人的短期摘要与最近事件（保留长期画像）",
        "- memory-lock <联系人>：锁定长期画像，防止后续误改",
        "- memory-unlock <联系人>：解锁长期画像",
        "",
        "帮助：",
        "- command：显示本帮助",
        "- /command：显示本帮助（聊天里更顺手）",
        "",
        "示例：",
        "- ./wechat_env/bin/python gateway_control.py queue",
        "- ./wechat_env/bin/python gateway_control.py since",
        "- ./wechat_env/bin/python gateway_control.py command",
        '- ./wechat_env/bin/python gateway_control.py style-set "自然、简短、口语化，不要句号"',
        '- ./wechat_env/bin/python gateway_control.py memory-set May "朋友，口语一点，可以轻微调侃，别太油"',
    ]
    return "\n".join(lines)


def reset_runtime_state(command: str = "restart") -> tuple[dict[str, Any], dict[str, Any]]:
    def stop_runner_processes() -> None:
        for target in (PROJECT_ROOT / "main.py", PROJECT_ROOT / "apps" / "runner" / "cli.py"):
            subprocess.run(
                ["pkill", "-f", str(target)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    config = load_config()
    before_state = load_state()
    before_queue = _pending_queue(before_state)
    append_event(
        "gateway_runtime_reset",
        command=command,
        pending_count_before=len(before_queue),
        pending_contacts_before=[str(item.get("contact") or "").strip() for item in before_queue if item.get("contact")],
        enabled_before=bool(config.get("enabled")),
    )
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
    append_event(
        "gateway_runtime_reset_done",
        command=command,
        pending_count_after=len(_pending_queue(state)),
        enabled_after=bool(config.get("enabled")),
    )
    return config, state


def main() -> int:
    args = parse_args()
    command = args.command
    if command in {"help", "/help", "command", "/command"}:
        command = "command"
    if command == "on":
        config = set_enabled(True)
        state = load_state()
    elif command == "off":
        config = set_enabled(False)
        state = load_state()
    elif command in {"reset", "restart"}:
        config, state = reset_runtime_state(command)
    elif command == "runner-start":
        config = load_config()
        if not bool(config.get("enabled")):
            config["enabled"] = True
            save_config(config)
        state = load_state()
        before = _runner_snapshot()
        if before["runner_online"]:
            after = before
            actions: list[str] = []
        else:
            after, actions = ensure_runner_started()
        append_event(
            "gateway_runner_start",
            before_runner_online=bool(before["runner_online"]),
            after_runner_online=bool(after["runner_online"]),
            actions=actions,
            enabled=bool(config.get("enabled")),
        )
    elif command == "style-set":
        config = load_config()
        config["reply_style_instructions"] = " ".join(args.style_text).strip()
        save_config(config)
        state = load_state()
    elif command == "memory-set":
        config = load_config()
        memory = set_contact_profile(args.contact_name, args.profile_text)
        append_event("gateway_memory_profile_set", contact=memory.get("contact"), profile=memory.get("profile"))
        state = load_state()
    elif command == "memory-clear":
        config = load_config()
        memory = clear_contact_recent_memory(args.contact_name)
        append_event("gateway_memory_recent_cleared", contact=memory.get("contact"))
        state = load_state()
    elif command == "memory-lock":
        config = load_config()
        memory = set_contact_profile_lock(args.contact_name, True)
        append_event("gateway_memory_profile_locked", contact=memory.get("contact"))
        state = load_state()
    elif command == "memory-unlock":
        config = load_config()
        memory = set_contact_profile_lock(args.contact_name, False)
        append_event("gateway_memory_profile_unlocked", contact=memory.get("contact"))
        state = load_state()
    else:
        config = load_config()
        if command not in {"style-show", "memory-show", "command", "runner"}:
            save_config(config)
        state = load_state()
    if command == "status":
        print(status_output(config, state))
    elif command == "runner":
        print(runner_output(config, state))
    elif command == "runner-start":
        print(runner_start_output(config, state, before, after, actions))
    elif command == "queue":
        print(queue_output(config, state))
    elif command in {"since", "sent-since"}:
        output, state = sent_since_output(config, state)
        print(output)
    elif command == "diagnose":
        print(diagnose_output(config, state))
    elif command == "style-show":
        print(style_show_output(config))
    elif command == "style-set":
        print("微信自动回复：语气规则已更新")
        print(style_show_output(config))
    elif command == "memory-show":
        print(memory_show_output(config, state, args.contact_name))
    elif command == "memory-set":
        print(f"微信自动回复：已更新 {memory.get('contact') or args.contact_name} 的长期画像")
        print(memory_show_output(config, state, str(memory.get("contact") or args.contact_name)))
    elif command == "memory-clear":
        print(f"微信自动回复：已清空 {memory.get('contact') or args.contact_name} 的短期记忆")
        print(memory_show_output(config, state, str(memory.get("contact") or args.contact_name)))
    elif command == "memory-lock":
        print(f"微信自动回复：已锁定 {memory.get('contact') or args.contact_name} 的长期画像")
        print(memory_show_output(config, state, str(memory.get("contact") or args.contact_name)))
    elif command == "memory-unlock":
        print(f"微信自动回复：已解锁 {memory.get('contact') or args.contact_name} 的长期画像")
        print(memory_show_output(config, state, str(memory.get("contact") or args.contact_name)))
    elif command in {"reset", "restart"}:
        print("微信自动回复：已重置并重启（待发送 0）")
    elif command == "command":
        print(help_output(config, state))
    else:
        pending_count = len(_pending_queue(state))
        print(status_line(config, pending_count))
    return 0
