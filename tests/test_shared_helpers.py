from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_autoreply.json_output import write_json_atomically
from wechat_autoreply.orchestrator import _meaningful_items


class SharedHelperTests(unittest.TestCase):
    def test_atomic_json_write_preserves_format_and_replaces_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            write_json_atomically(path, {"old": True})
            payload = {"message": "消息", "queue": [1, 2]}
            write_json_atomically(path, payload)
            self.assertEqual(path.read_text(), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_serialization_does_not_replace_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_json_atomically(path, {"previous": True})
            with self.assertRaises(TypeError):
                write_json_atomically(path, {"invalid": object()})
            self.assertEqual(json.loads(path.read_text()), {"previous": True})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_replace_preserves_state_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_json_atomically(path, {"previous": True})
            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    write_json_atomically(path, {"next": True})
            self.assertEqual(json.loads(path.read_text()), {"previous": True})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_write_preserves_state_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_json_atomically(path, {"previous": True})
            with patch.object(Path, "write_text", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_json_atomically(path, {"next": True})
            self.assertEqual(json.loads(path.read_text()), {"previous": True})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_bubble_selection_keeps_roles_order_and_independent_copies(self) -> None:
        panel = {
            "inbound": [
                {"text": " 晚点出发 ", "top": "0.7", "metadata": {"id": 2}},
                {"text": "", "top": 0.1},
                {"text": "今天几点出发", "top": 0.3},
            ],
            "outbound": [{"text": "我晚点回你", "top": 0.5}],
        }
        original = copy.deepcopy(panel)
        inbound = _meaningful_items(panel, "inbound")
        outbound = _meaningful_items(panel, "outbound")
        self.assertEqual([item["text"] for item in inbound], ["今天几点出发", "晚点出发"])
        self.assertEqual([item["text"] for item in outbound], ["我晚点回你"])
        self.assertEqual(inbound[1]["top"], 0.7)
        inbound[1]["metadata"]["id"] = 99
        self.assertEqual(panel, original)
        self.assertEqual(_meaningful_items({}, "inbound"), [])
