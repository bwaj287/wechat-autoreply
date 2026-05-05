from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = PROJECT_ROOT / "runtime"
CAPTURE_DIR = RUNTIME_DIR / "captures"
DEBUG_DIR = RUNTIME_DIR / "debug"
LOG_DIR = RUNTIME_DIR / "logs"
TOOLS_DIR = PROJECT_ROOT / "tools"

CONFIG_PATH = RUNTIME_DIR / "config.json"
EVENTS_PATH = RUNTIME_DIR / "events.jsonl"
CONTACT_MEMORY_PATH = RUNTIME_DIR / "contact_memory.json"
LOCK_PATH = RUNTIME_DIR / "runner.lock"
WHITELIST_PATH = Path("/Users/shawnwang/Documents/Playground/wechat-whitelist.txt")
SWITCH_PATH = Path("/Users/shawnwang/Documents/Playground/wechat-auto-reply-switch.txt")
STATE_PATH = Path("/Users/shawnwang/.openclaw/workspace/wechat-auto-reply-state.json")

PEEKABOO = Path("/opt/homebrew/bin/peekaboo")
BRIDGE_SOCKET = Path.home() / "Library" / "Application Support" / "Peekaboo" / "bridge.sock"
WECHAT_APP = "WeChat"
OCR_HELPER = TOOLS_DIR / "wechat_ocr.swift"
ROW_BADGE_HELPER = TOOLS_DIR / "wechat_row_badges.swift"
BUBBLE_ROLE_HELPER = TOOLS_DIR / "wechat_bubble_roles.swift"


def ensure_runtime_dirs() -> None:
    for path in (
        RUNTIME_DIR,
        CAPTURE_DIR,
        DEBUG_DIR,
        LOG_DIR,
        STATE_PATH.parent,
        WHITELIST_PATH.parent,
        SWITCH_PATH.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)
