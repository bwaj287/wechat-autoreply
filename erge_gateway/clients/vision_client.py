from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import requests

from erge_gateway.cache import JsonFileCache
from erge_gateway.config import Settings
from erge_gateway.schemas import VisionSummary


class VisionClient:
    def __init__(self, settings: Settings, cache: JsonFileCache) -> None:
        self.settings = settings
        self.cache = cache

    def ocr_image(self, image_path: Path) -> list[dict[str, Any]]:
        proc = subprocess.run(
            ["swift", self.settings.ocr_helper_path, str(image_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        payload = json.loads(proc.stdout or "{}")
        return list(payload.get("results", []))

    def _vision_available(self) -> bool:
        try:
            response = requests.get(
                f"{self.settings.vision_base_url}/api/tags",
                timeout=self.settings.connect_timeout_seconds,
            )
            response.raise_for_status()
            models = response.json().get("models", [])
            return any(str(model.get("name") or "") == self.settings.vision_model for model in models)
        except Exception:
            return False

    def _summarize_with_model(self, image_path: Path, visible_text: str) -> VisionSummary:
        prompt = (
            "You are a vision preprocessor for a larger reasoning model. "
            "Return compact JSON with keys summary, entities, uncertainties. "
            "Do not answer the user question directly. Focus on visible objects, scene, layout, and caveats. "
            f"OCR text:\n{visible_text or '(none)'}"
        )
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        response = requests.post(
            f"{self.settings.vision_base_url}/api/generate",
            json={
                "model": self.settings.vision_model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "format": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "entities": {"type": "array", "items": {"type": "string"}},
                        "uncertainties": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["summary", "entities", "uncertainties"],
                },
                "images": [encoded],
                "options": {"temperature": 0.1, "num_predict": 220},
            },
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        raw = str(body.get("response") or "").strip()
        if not raw:
            raw = str(body.get("thinking") or "").strip()

        if raw.startswith("<think>"):
            raw = re.sub(r"^<think>\s*", "", raw, flags=re.DOTALL).strip()
        if raw.endswith("</think>"):
            raw = re.sub(r"\s*</think>$", "", raw, flags=re.DOTALL).strip()

        if raw and not raw.startswith("{"):
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                raw = match.group(0).strip()

        parsed = json.loads(raw or "{}")
        return VisionSummary(
            summary=str(parsed.get("summary") or "").strip(),
            visible_text=visible_text,
            entities=[str(item).strip() for item in parsed.get("entities") or [] if str(item).strip()],
            uncertainties=[str(item).strip() for item in parsed.get("uncertainties") or [] if str(item).strip()],
        )

    def summarize_image(self, image_path: Path) -> tuple[VisionSummary, bool]:
        key = JsonFileCache.stable_key(str(image_path), str(image_path.stat().st_mtime_ns), str(image_path.stat().st_size))
        cached = self.cache.get("vision", key)
        if cached:
            return VisionSummary.model_validate(cached), True

        ocr_results = self.ocr_image(image_path)
        visible_text = "\n".join(str(item.get("text") or "").strip() for item in ocr_results if str(item.get("text") or "").strip())
        if self._vision_available():
            try:
                summary = self._summarize_with_model(image_path, visible_text)
            except Exception as exc:
                summary = VisionSummary(
                    summary="",
                    visible_text=visible_text,
                    entities=[],
                    uncertainties=[f"vision_model_error: {exc}"],
                )
        else:
            summary = VisionSummary(
                summary="",
                visible_text=visible_text,
                entities=[],
                uncertainties=["vision_model_unavailable"],
            )

        self.cache.put("vision", key, summary.model_dump())
        return summary, False
