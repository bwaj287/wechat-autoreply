import json
import tempfile
from pathlib import Path
from typing import Any

from .paths import CONFIG_PATH, ensure_runtime_dirs


def _archive_whitelist_candidates() -> list[Path]:
    archive_root = Path.home() / ".openclaw" / "workspace" / ".reset-archive"
    return sorted(archive_root.glob("wechat-auto-reply-reset-*/documents/wechat-whitelist.txt"))


def seed_allowed_contacts() -> list[str]:
    for candidate in reversed(_archive_whitelist_candidates()):
        if not candidate.exists():
            continue
        contacts: list[str] = []
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            contacts.append(line)
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
        "send_delay_seconds": 300,
        "pending_refresh_delay_seconds": 180,
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
        "capture_retention_days": 1,
        "passive_roster_sweep_enabled": False,
        "roster_sweep_interval_seconds": 60,
        "sweep_while_pending": False,
        "allowed_contacts": seed_allowed_contacts(),
        "ollama_model": "qwen3.5:9b",
        "ollama_url": "http://127.0.0.1:11434/api/generate",
        "max_reply_chars": 90,
        "emoji_pack_zip_path": str(Path.home() / "Downloads" / "wechat-emoji-main.zip"),
        "reply_emoji_enabled": True,
        "reply_emoji_min_count": 1,
        "reply_emoji_max_count": 2,
        "reply_style_instructions": (
            "Write like Shawn texting on WeChat. "
            "Natural, casual, short, and human. "
            "Do not sound like customer support or an AI assistant. "
            "Prefer direct wording over polite filler. "
            "Do not explain yourself. "
            "Do not use bullet points. "
            "Do not use quotes around the reply. "
            "Omit sentence-final periods in each reply. "
            "Use WeChat emoji codes naturally, usually 1-2 per reply (for example [捂脸] [旺柴]). "
            "Avoid phrases like 当然可以, 好的我来帮你, 根据你的需求, 很高兴为你服务. "
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
        _atomic_write(CONFIG_PATH, cfg)
        return cfg
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = default_config()
    merged.update(cfg)
    if merged != cfg:
        _atomic_write(CONFIG_PATH, merged)
    return merged


def save_config(config: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    merged = default_config()
    merged.update(config)
    _atomic_write(CONFIG_PATH, merged)


def set_enabled(enabled: bool) -> dict[str, Any]:
    config = load_config()
    config["enabled"] = bool(enabled)
    save_config(config)
    return config


def status_line(config: dict[str, Any], pending_count: int) -> str:
    state = "已开启" if config.get("enabled") else "已关闭"
    return f"微信自动回复：{state}（待发送 {pending_count}）"
