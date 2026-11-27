from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from wechat_autoreply import ocr, wechat_ui


class OcrHelperRecoveryTests(unittest.TestCase):
    def test_outbound_recovery_adds_only_missing_green_bubbles(self) -> None:
        def observation(text, x, y):
            return {"text": text, "confidence": 1.0, "bbox": {"x": x, "y": y, "w": .2, "h": .03}}

        base = [observation("existing", .65, .4)]
        recovered = [base[0], observation("missed reply", .65, .2),
                     observation("input placeholder", .35, .13), observation("uncertain", .4, .3)]

        def annotate(items, path):
            for item in items:
                item["bubbleRole"] = {"existing": "outbound", "missed reply": "outbound",
                                      "input placeholder": "inbound"}.get(item["text"], "unknown")

        for enabled in (False, True):
            with self.subTest(enabled=enabled), patch.object(wechat_ui, "ocr_image", return_value=base), patch.object(
                wechat_ui, "ocr_region", return_value=recovered
            ) as region, patch.object(wechat_ui, "_annotate_bubble_roles", side_effect=annotate):
                items = wechat_ui._prepare_ocr_items(Path("unused.png"), recover_outbound=enabled)
            self.assertEqual([item["text"] for item in items], ["existing", "missed reply"] if enabled else ["existing"])
            self.assertEqual(region.call_count, int(enabled))
            if enabled:
                panel = wechat_ui._extract_chat_window_panel(items)
                self.assertEqual(panel["latestOutbound"], "missed reply")
                self.assertEqual(panel["latestInbound"], "")

    def test_outbound_recovery_failure_preserves_base_read(self) -> None:
        base = [{"text": "original", "confidence": 1, "bbox": {"x": .6, "y": .2, "w": .2, "h": .03}}]
        with patch.object(wechat_ui, "ocr_image", return_value=base), patch.object(
            wechat_ui, "ocr_region", side_effect=RuntimeError("crop unreadable")
        ), patch.object(wechat_ui, "_annotate_bubble_roles"), patch.object(wechat_ui, "append_event") as event:
            items = wechat_ui._prepare_ocr_items(Path("unused.png"), recover_outbound=True)
        self.assertEqual([item["text"] for item in items], ["original"])
        self.assertEqual(event.call_args.args[0], "ocr_outbound_recovery_failed")

    def test_contact_candidates_do_not_guess_or_override_known_names(self) -> None:
        obs = {"text": "Mav", "confidence": .9,
               "candidates": [{"text": "May", "confidence": .85}]}
        self.assertEqual(ocr.contact_label(obs, ["May"]), "May")
        self.assertEqual(obs["text"], "Mav")
        self.assertEqual(ocr.contact_label(obs, ["May", "Mav"]), "Mav")
        self.assertEqual(ocr.contact_label(dict(obs, candidates=[]), ["May"]), "Mav")
        self.assertEqual(ocr.contact_label(dict(obs, text="Servic..."), ["May"]), "Servic...")
        self.assertEqual(ocr.contact_label(dict(obs, text="Unrelated"), ["May"]), "Unrelated")
        obs["candidates"].append({"text": "Mary", "confidence": .85})
        self.assertEqual(ocr.contact_label(obs, ["May", "Mary"]), "Mav")
        self.assertEqual(ocr.contact_label(dict(obs, text="Mar", confidence=1), ["Mary"]), "Mary")
        obs["candidates"][0]["confidence"] = .2
        self.assertEqual(ocr.contact_label(obs, ["May"]), "Mav")

    def test_name_resolution_does_not_rewrite_body(self) -> None:
        original = [{"text": "Mav", "confidence": .9, "candidates": [{"text": "May", "confidence": .9}],
                     "bbox": {"x": .34, "y": .45, "w": .08, "h": .03}}]
        with patch.object(wechat_ui, "ocr_image", return_value=original), patch.object(
            wechat_ui, "load_allowed_contacts", return_value=["May"]
        ), patch.object(wechat_ui, "_annotate_bubble_roles"):
            result = wechat_ui._prepare_ocr_items(Path("unused.png"))
        result[0]["bubbleRole"] = "inbound"
        panel = wechat_ui._extract_chat_panel(result)
        self.assertEqual(result[0]["contactText"], "May")
        self.assertEqual(panel["latestInbound"], "Mav")

    def test_region_remaps_bottom_left_boxes_and_removes_temporary_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.png"
            Image.new("RGB", (100, 200)).save(path)
            with patch.object(ocr, "ocr_image", return_value=[
                {"text": "May", "bbox": {"x": .1, "y": .2, "w": .4, "h": .3}}
            ]) as recognize:
                result = ocr.ocr_region(path, (.2, .1, .8, .6), custom_words=("May",))
            box = result[0]["bbox"]
            for key, value in {"x": .26, "y": .5, "w": .24, "h": .15}.items():
                self.assertAlmostEqual(box[key], value)
            self.assertEqual(recognize.call_args.kwargs["custom_words"], ("May",))
            self.assertFalse(recognize.call_args.args[0].exists())
            with self.assertRaises(ValueError):
                ocr.ocr_region(path, (.8, .1, .2, .6))

    def test_optional_enhancement_failure_preserves_base_ocr(self) -> None:
        original = [{"text": "May", "bbox": {"x": 0.1, "y": 0.8, "w": 0.1, "h": 0.02}, "confidence": 0.9}]
        with tempfile.TemporaryDirectory() as tmp:
            variants = [Path(tmp) / "contrast.png", Path(tmp) / "inverted.png"]
            for path in variants:
                path.touch()
            with patch.object(wechat_ui, "_build_roster_ocr_variants", return_value=variants), patch.object(
                wechat_ui, "ocr_image", side_effect=[original, RuntimeError("enhancement failed"), original]
            ), patch.object(wechat_ui, "_annotate_bubble_roles"), patch.object(wechat_ui, "append_event") as event:
                result = wechat_ui._prepare_ocr_items(Path(tmp) / "base.png", panel_hint="roster")
            self.assertEqual([item["text"] for item in result], ["May"])
            self.assertTrue(all(not path.exists() for path in variants))
            self.assertEqual(event.call_args.args[0], "ocr_enhancement_failed")

    def test_base_ocr_failure_is_not_hidden(self) -> None:
        with patch.object(wechat_ui, "ocr_image", side_effect=RuntimeError("base failed")):
            with self.assertRaisesRegex(RuntimeError, "base failed"):
                wechat_ui._prepare_ocr_items(Path("base.png"))

    def test_ocr_error_includes_bounded_stderr(self) -> None:
        failure = subprocess.CalledProcessError(1, ["swift"], stderr="cannot load image " + "x" * 1000)
        with patch.object(ocr, "_ensure_tracked_helper_local"), patch.object(ocr.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "cannot load image") as caught:
                ocr.ocr_image(Path("base.png"))
        self.assertLess(len(str(caught.exception)), 700)

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
