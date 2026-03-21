from __future__ import annotations

import re

import requests


def _same_language_hint(text: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", text or ""):
        return "Reply in Chinese."
    return "Reply in English."


class OllamaClient:
    def __init__(
        self,
        url: str,
        model: str,
        max_reply_chars: int = 90,
        style_instructions: str = "",
    ) -> None:
        self.url = url
        self.model = model
        self.max_reply_chars = max_reply_chars
        self.style_instructions = str(style_instructions or "").strip()

    def generate_reply(self, contact: str, inbound_text: str) -> str:
        num_predict = max(12, min(48, self.max_reply_chars // 2))
        style_block = f"{self.style_instructions}\n" if self.style_instructions else ""
        prompt = (
            "You write short, natural WeChat replies.\n"
            f"{_same_language_hint(inbound_text)}\n"
            f"{style_block}"
            "Keep it colloquial and human.\n"
            "Do not explain yourself.\n"
            "Do not use bullet points.\n"
            "Do not use quotes around the reply.\n"
            f"Stay under {self.max_reply_chars} characters.\n\n"
            f"Contact: {contact}\n"
            f"Incoming message: {inbound_text}\n\n"
            "Reply:"
        )
        response = requests.post(
            self.url,
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {
                    "num_ctx": 1024,
                    "num_predict": num_predict,
                    "temperature": 0.7,
                },
            },
            timeout=(5, 120),
        )
        response.raise_for_status()
        payload = response.json()
        text = str(payload.get("response", "")).strip()
        if not text:
            raise RuntimeError("ollama returned an empty reply")
        return text[: self.max_reply_chars].strip()
