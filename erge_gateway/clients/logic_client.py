from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import requests
from urllib3.exceptions import ReadTimeoutError

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
        started = time.perf_counter()
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
        latency_ms = int((time.perf_counter() - started) * 1000)
        if latency_ms > int(self.settings.busy_threshold_seconds * 1000):
            return LogicHealth(status="healthy", latency_ms=latency_ms, reason="pc_model_ready_slow")
        return LogicHealth(status="healthy", latency_ms=latency_ms, reason="pc_model_ready")

    def _primary_chat(self, messages: list[dict[str, str]]) -> LogicResult:
        start_timeout = self.settings.primary_start_timeout_seconds
        payload = self._chat_payload(self.settings.logic_primary_model, messages)
        payload["stream"] = True
        started = time.monotonic()
        chunks: list[str] = []
        done = False
        with requests.post(
            f"{self.settings.logic_primary_base_url}/api/chat",
            json=payload,
            stream=True,
            timeout=(self.settings.connect_timeout_seconds, start_timeout),
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines(chunk_size=1):
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise requests.RequestException("Invalid PC stream event") from exc
                if event.get("error"):
                    raise requests.RequestException(str(event["error"]))
                content = str(event.get("message", {}).get("content") or "")
                if not chunks and time.monotonic() - started > start_timeout:
                    raise requests.Timeout("PC did not start answering before the deadline")
                if content:
                    chunks.append(content)
                if event.get("done"):
                    done = True
                    break
        if not done or not "".join(chunks).strip():
            raise requests.RequestException("PC returned no complete readable answer")
        return LogicResult(content="".join(chunks).strip(), backend="pc_5080")

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
        except requests.RequestException as exc:
            reason = "pc_timeout" if isinstance(exc.__context__, ReadTimeoutError) else "pc_infer_failed"
            return self._local_chat(normalized, fallback_reason=reason)
