from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = PROJECT_ROOT / "runtime"
CAPTURE_DIR = RUNTIME_DIR / "captures"
LOG_DIR = RUNTIME_DIR / "logs"
TOOLS_DIR = PROJECT_ROOT / "tools"

CONFIG_PATH = RUNTIME_DIR / "config.json"
STATE_PATH = RUNTIME_DIR / "state.json"
EVENTS_PATH = RUNTIME_DIR / "events.jsonl"
LOCK_PATH = RUNTIME_DIR / "runner.lock"

PEEKABOO = Path("/opt/homebrew/bin/peekaboo")
BRIDGE_SOCKET = Path.home() / "Library" / "Application Support" / "Peekaboo" / "bridge.sock"
WECHAT_APP = "WeChat"
OCR_HELPER = TOOLS_DIR / "wechat_ocr.swift"
ROW_BADGE_HELPER = TOOLS_DIR / "wechat_row_badges.swift"
BUBBLE_ROLE_HELPER = TOOLS_DIR / "wechat_bubble_roles.swift"


def ensure_runtime_dirs() -> None:
    for path in (RUNTIME_DIR, CAPTURE_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
