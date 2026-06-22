from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from wechat_autoreply import capture_cleanup, event_log


class EventLogTests(unittest.TestCase):
    def test_append_event_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            lock_path = Path(tmp) / "events.lock"
            with (
                patch.object(event_log, "EVENTS_PATH", events_path),
                patch.object(event_log, "EVENTS_LOCK_PATH", lock_path),
            ):
                event_log.append_event("test_event", value=3)
            payload = json.loads(events_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["type"], "test_event")
            self.assertEqual(payload["value"], 3)

    def test_primary_failure_falls_back_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fallback_path = Path(tmp) / "fallback.jsonl"
            with (
                patch.object(event_log, "FALLBACK_EVENTS_PATH", fallback_path),
                patch.object(event_log, "_append_primary_with_retry", side_effect=OSError("busy")),
            ):
                event_log.append_event("fallback_event", contact="Darren")
            payload = json.loads(fallback_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["type"], "fallback_event")
            self.assertTrue(payload["event_log_fallback"])

    def test_append_and_prune_share_one_lock_without_corrupting_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            lock_path = Path(tmp) / "events.lock"
            events_path.write_text(
                '{"ts":"2000-01-01T00:00:00+00:00","type":"expired"}\n',
                encoding="utf-8",
            )

            def append_many() -> None:
                for index in range(50):
                    event_log.append_event("concurrent_append", index=index)

            def prune_many() -> None:
                for _ in range(10):
                    capture_cleanup._prune_events_file(
                        older_than_seconds=24 * 60 * 60,
                        now=time.time(),
                        events_path=events_path,
                    )

            with (
                patch.object(event_log, "EVENTS_PATH", events_path),
                patch.object(event_log, "EVENTS_LOCK_PATH", lock_path),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                futures = [pool.submit(append_many), pool.submit(prune_many)]
                for future in futures:
                    future.result(timeout=5)

            payloads = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            appended = [payload for payload in payloads if payload.get("type") == "concurrent_append"]
            self.assertEqual(len(appended), 50)
            self.assertFalse(any(payload.get("type") == "expired" for payload in payloads))


if __name__ == "__main__":
    unittest.main()
