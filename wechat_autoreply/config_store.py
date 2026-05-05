import json
import tempfile
from pathlib import Path
from typing import Any

from .paths import CONFIG_PATH, SWITCH_PATH, WHITELIST_PATH, ensure_runtime_dirs


LOCAL_OLLAMA_MODEL = "qwen3.5:9b"
LOCAL_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"


def _archive_whitelist_candidates() -> list[Path]:
    archive_root = Path.home() / ".openclaw" / "workspace" / ".reset-archive"
    return sorted(archive_root.glob("wechat-auto-reply-reset-*/documents/wechat-whitelist.txt"))


def _parse_contacts(lines: list[str]) -> list[str]:
    contacts: list[str] = []
    seen: set[str] = set()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        contacts.append(line)
    return contacts


def _parse_switch(raw: str) -> bool | None:
    value = str(raw or "").strip().lower()
    if value in {"on", "1", "true", "enabled"}:
        return True
    if value in {"off", "0", "false", "disabled"}:
        return False
    return None


def _read_switch(default_enabled: bool) -> bool:
    ensure_runtime_dirs()
    if SWITCH_PATH.exists():
        for raw_line in SWITCH_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parsed = _parse_switch(line)
            if parsed is not None:
                return parsed
            break
    _write_switch(default_enabled)
    return bool(default_enabled)


def _write_switch(enabled: bool) -> None:
    ensure_runtime_dirs()
    SWITCH_PATH.write_text(("on" if enabled else "off") + "\n", encoding="utf-8")


def _write_whitelist_if_missing(default_contacts: list[str]) -> None:
    ensure_runtime_dirs()
    if WHITELIST_PATH.exists():
        return
    content = "\n".join(
        [
            "# WeChat auto-reply whitelist",
            "# One contact per line. Lines starting with # are ignored.",
            *default_contacts,
            "",
        ]
    )
    WHITELIST_PATH.write_text(content, encoding="utf-8")


def load_allowed_contacts(default_contacts: list[str] | None = None) -> list[str]:
    ensure_runtime_dirs()
    if WHITELIST_PATH.exists():
        return _parse_contacts(WHITELIST_PATH.read_text(encoding="utf-8").splitlines())
    fallback = _parse_contacts(list(default_contacts or []))
    if not fallback:
        fallback = _parse_contacts(seed_allowed_contacts())
    _write_whitelist_if_missing(fallback)
    return fallback


def seed_allowed_contacts() -> list[str]:
    if WHITELIST_PATH.exists():
        return _parse_contacts(WHITELIST_PATH.read_text(encoding="utf-8").splitlines())
    for candidate in reversed(_archive_whitelist_candidates()):
        if not candidate.exists():
            continue
        contacts = _parse_contacts(candidate.read_text(encoding="utf-8").splitlines())
        if contacts:
            return contacts
    return [
        "测试白名单",
        "家人",
        "客户A",
        "shawn",
        "May",
        "Darren",
        "Barrys",
        "1ock",
        "可乐",
        "Ted Liu",
        "王哥",
        "刘若愚",
    ]


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "enabled": False,
        "idle_threshold_seconds": 30,
        "send_delay_seconds": 180,
        "pending_refresh_delay_seconds": 180,
        "pending_change_debounce_frames": 3,
        "pending_change_min_votes": 2,
        "pending_change_similarity_threshold": 0.9,
        "badge_stability_frames": 2,
        "non_whitelist_badge_stability_frames": 1,
        "send_verify_retry_seconds": 45,
        "recheck_vote_frames": 1,
        "recheck_vote_interval_seconds": 0.25,
        "recheck_min_confidence": 0.0,
        "recheck_low_confidence_delay_seconds": 60,
        "recheck_low_confidence_max_delay_seconds": 900,
        "recheck_low_confidence_max_retries": 4,
        "recheck_low_confidence_snooze_seconds": 1800,
        "recheck_tail_min_top": 0.52,
        "recheck_tail_span": 0.28,
        "send_max_attempts": 2,
        "pending_stale_ttl_seconds": 86400,
        "poll_interval_seconds": 5,
        "menubar_check_interval_seconds": 15,
        "capture_cleanup_interval_seconds": 3600,
        "capture_retention_days": 2,
        "preferred_roster_window_bounds": {},
        "passive_roster_sweep_enabled": False,
        "roster_sweep_interval_seconds": 60,
        "sweep_while_pending": False,
        "allowed_contacts": seed_allowed_contacts(),
        "ollama_model": LOCAL_OLLAMA_MODEL,
        "ollama_url": LOCAL_OLLAMA_URL,
        "erge_enabled": True,
        "erge_model": "brother",
        "erge_gateway_url": "http://127.0.0.1:4010/v1/chat/completions",
        "erge_health_url": "http://127.0.0.1:4010/health",
        "erge_health_timeout_seconds": 6,
        "erge_health_cache_seconds": 15,
        "erge_request_timeout_seconds": 120,
        "max_reply_chars": 90,
        "reply_context_messages": 8,
        "contact_memory_enabled": True,
        "contact_memory_max_events": 18,
        "contact_memory_retention_days": 14,
        "emoji_pack_zip_path": str(Path.home() / "Downloads" / "wechat-emoji-main.zip"),
        "reply_emoji_enabled": False,
        "reply_emoji_min_count": 0,
        "reply_emoji_max_count": 0,
        "reply_style_instructions": (
            "Write like Shawn texting on WeChat. "
            "Sound casual, relaxed, and direct, like chatting with friends. "
            "Keep replies short and natural. "
            "Do not sound like customer support or an AI assistant. "
            "Do not be overly polite, eager, or explanatory. "
            "A little teasing or playful tone is fine, but do not sound greasy. "
            "Prefer simple spoken Chinese over polished written language. "
            "Do not use bullet points. "
            "Do not use quotes around the reply. "
            "Omit sentence-final periods in each reply. "
            "Do not proactively use emojis; default to no emojis unless omitting one would feel obviously awkward. "
            "Avoid phrases like 当然可以, 好的呀, 很高兴为你服务, 根据你的需求, 希望对你有帮助. "
            "If the incoming message is in Chinese, reply in natural spoken Chinese."
        ),
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def load_config() -> dict[str, Any]:
    ensure_runtime_dirs()
    if not CONFIG_PATH.exists():
        cfg = default_config()
        cfg["enabled"] = _read_switch(bool(cfg.get("enabled")))
        cfg["allowed_contacts"] = load_allowed_contacts(list(cfg.get("allowed_contacts", [])))
        _atomic_write(CONFIG_PATH, cfg)
        return cfg
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = default_config()
    merged.update(cfg)
    merged["ollama_model"] = LOCAL_OLLAMA_MODEL
    merged["ollama_url"] = LOCAL_OLLAMA_URL
    merged["enabled"] = _read_switch(bool(merged.get("enabled")))
    merged["allowed_contacts"] = load_allowed_contacts(list(merged.get("allowed_contacts", [])))
    if merged != cfg:
        _atomic_write(CONFIG_PATH, merged)
    return merged


def save_config(config: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    merged = default_config()
    merged.update(config)
    merged["ollama_model"] = LOCAL_OLLAMA_MODEL
    merged["ollama_url"] = LOCAL_OLLAMA_URL
    enabled_value = bool(config.get("enabled")) if "enabled" in config else _read_switch(bool(merged.get("enabled")))
    _write_switch(enabled_value)
    merged["enabled"] = enabled_value
    merged["allowed_contacts"] = load_allowed_contacts(list(merged.get("allowed_contacts", [])))
    _atomic_write(CONFIG_PATH, merged)


def set_enabled(enabled: bool) -> dict[str, Any]:
    config = load_config()
    config["enabled"] = bool(enabled)
    save_config(config)
    return config


def status_line(config: dict[str, Any], pending_count: int) -> str:
    state = "已开启" if config.get("enabled") else "已关闭"
    return f"微信自动回复：{state}（待发送 {pending_count}）"
