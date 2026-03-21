#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from pathlib import Path

from wechat_autoreply.config_store import load_config
from wechat_autoreply.orchestrator import AutoReplyRunner
from wechat_autoreply.paths import LOCK_PATH, ensure_runtime_dirs


def acquire_lock() -> tuple[Path, object]:
    ensure_runtime_dirs()
    handle = LOCK_PATH.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("wechat autoreply runner is already running")
    handle.write(str(Path.cwd()) + "\n")
    handle.flush()
    return LOCK_PATH, handle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the WeChat auto-reply V1 runner.")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--dry-run", action="store_true", help="never paste or send a real reply")
    parser.add_argument("--json", action="store_true", help="print the tick result as JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _lock_path, lock_handle = acquire_lock()
    runner = AutoReplyRunner(dry_run=args.dry_run)

    try:
        if args.once:
            result = runner.tick()
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(result)
            return 0

        while True:
            result = runner.tick()
            print(json.dumps(result, ensure_ascii=False))
            interval = float(load_config().get("poll_interval_seconds", 5))
            time.sleep(max(interval, 1.0))
    finally:
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
