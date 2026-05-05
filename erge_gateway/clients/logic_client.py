from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from erge_gateway.config import Settings


@dataclass(slots=True)
class LogicHealth:
    status: str
    latency_ms: int | None
    reason: str


@dataclass(slots=True)
class LogicResult:
    content: str
    backend: str
    fallback_reason: str | None = None


class LogicClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _is_game_mode_locked(self) -> bool:
        url = str(self.settings.game_mode_url or "").strip()
        if not url:
            return False
        try:
            response = requests.get(
                url,
                timeout=self.settings.game_mode_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            return bool(payload.get("game_mode") is True)
        except Exception:
            return False

    def _chat_payload(self, model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": model,
            "stream": False,
            "think": False,
            "messages": messages,
            "options": {"temperature": 0.2},
        }

    @staticmethod
    def _part_to_text(part: Any) -> str:
        if not isinstance(part, dict):
            return ""
        kind = str(part.get("type") or "").strip()
        if kind == "text":
            return str(part.get("text") or "").strip()
        if kind in {"image_url", "input_image"}:
            return "[Image attached]"
        if kind in {"file_url", "input_file", "file"}:
            raw = part.get("file_url") or part.get("file") or ""
            if isinstance(raw, dict):
                name = str(raw.get("file_name") or raw.get("filename") or raw.get("url") or "").strip()
            else:
                name = str(raw).strip()
            return f"[File attached: {name}]" if name else "[File attached]"
        return ""

    def _normalize_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for raw in messages:
            role = str(raw.get("role") or "user").strip() or "user"
            content = raw.get("content")
            text = ""
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                parts = [self._part_to_text(part) for part in content]
                text = "\n".join(part for part in parts if part).strip()
            elif content is None:
                text = ""
            else:
                text = str(content).strip()
            if not text:
                text = "[No content]"
            normalized.append({"role": role, "content": text})
        return normalized

    def probe_primary(self) -> LogicHealth:
        if self._is_game_mode_locked():
            return LogicHealth(status="down", latency_ms=None, reason="pc_game_mode")
        try:
            tags = requests.get(
                f"{self.settings.health_pc_base_url}/api/tags",
                timeout=self.settings.connect_timeout_seconds,
            )
            tags.raise_for_status()
            models = tags.json().get("models", [])
            if not any(str(model.get("name") or "") == self.settings.health_pc_model for model in models):
                return LogicHealth(status="down", latency_ms=None, reason="pc_model_missing")
        except Exception:
            return LogicHealth(status="down", latency_ms=None, reason="pc_unreachable")

        started = time.perf_counter()
        try:
            response = requests.post(
                f"{self.settings.health_pc_base_url}/api/generate",
                json={
                    "model": self.settings.health_pc_model,
                    "prompt": "ping",
                    "stream": False,
                    "think": False,
                    "options": {"num_predict": 8, "temperature": 0},
                },
                timeout=self.settings.probe_timeout_seconds,
            )
            response.raise_for_status()
        except requests.Timeout:
            return LogicHealth(status="down", latency_ms=int((time.perf_counter() - started) * 1000), reason="pc_timeout")
        except Exception:
            return LogicHealth(status="down", latency_ms=int((time.perf_counter() - started) * 1000), reason="pc_infer_failed")

        latency_ms = int((time.perf_counter() - started) * 1000)
        if latency_ms > int(self.settings.probe_timeout_seconds * 1000):
            return LogicHealth(status="down", latency_ms=latency_ms, reason="pc_timeout")
        if latency_ms > int(self.settings.busy_threshold_seconds * 1000):
            return LogicHealth(status="degraded", latency_ms=latency_ms, reason="pc_slow")
        return LogicHealth(status="healthy", latency_ms=latency_ms, reason="pc_healthy")

    def _primary_chat(self, messages: list[dict[str, str]]) -> LogicResult:
        response = requests.post(
            f"{self.settings.logic_primary_base_url}/api/chat",
            json=self._chat_payload(self.settings.logic_primary_model, messages),
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload.get("message", {}).get("content", "")
        return LogicResult(content=str(content).strip(), backend="pc_5080")

    def _local_chat(self, messages: list[dict[str, str]], *, fallback_reason: str | None = None) -> LogicResult:
        response = requests.post(
            f"{self.settings.logic_local_base_url}/api/chat",
            json=self._chat_payload(self.settings.logic_local_model, messages),
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload.get("message", {}).get("content", "")
        return LogicResult(content=str(content).strip(), backend="local_small", fallback_reason=fallback_reason)

    def chat(self, messages: list[dict[str, Any]], prefer_primary: bool) -> LogicResult:
        normalized = self._normalize_messages(messages)
        if self._is_game_mode_locked():
            return self._local_chat(normalized, fallback_reason="pc_game_mode")
        if not prefer_primary:
            return self._local_chat(normalized)
        try:
            return self._primary_chat(normalized)
        except requests.Timeout:
            return self._local_chat(normalized, fallback_reason="pc_timeout")
        except requests.RequestException:
            return self._local_chat(normalized, fallback_reason="pc_infer_failed")
