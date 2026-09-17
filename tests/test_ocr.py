from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from wechat_autoreply import ocr


class OcrHelperRecoveryTests(unittest.TestCase):
    def test_dataless_helper_is_restored_from_current_git_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            helper = project_root / "tools" / "wechat_ocr.swift"
            helper.parent.mkdir(parents=True)
            helper.write_text("placeholder", encoding="utf-8")
            restored_content = b"#!/usr/bin/env swift\nprint(\"ok\")\n"
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=restored_content, stderr=b"")

            with (
                patch.object(ocr, "_is_dataless", return_value=True),
                patch.object(ocr.subprocess, "run", return_value=completed) as run,
            ):
                recovered = ocr._ensure_tracked_helper_local(helper, project_root)

            self.assertTrue(recovered)
            self.assertEqual(helper.read_bytes(), restored_content)
            self.assertEqual(
                run.call_args.args[0],
                ["git", "-C", str(project_root), "show", "HEAD:tools/wechat_ocr.swift"],
            )


if __name__ == "__main__":
    unittest.main()
