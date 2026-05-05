from __future__ import annotations

import hashlib
import re

import requests

from .emoji_library import build_wechat_emoji_codes, load_wechat_emoji_names


_WECHAT_CODE_RE = re.compile(r"\[[^\[\]\s]{1,12}\]")
_UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001FAFF"
    "\u2600-\u27BF"
    "]"
)
_SENTENCE_FINAL_PERIOD_RE = re.compile(r"[。．.]+$")
_LEADING_META_RE = re.compile(r"^(?:reply|回复|answer|assistant)\s*[:：]\s*", re.IGNORECASE)
_QUESTION_HINT_RE = re.compile(r"[?？]|(吗|嘛|呢|么|why|what|how|where|when)", re.IGNORECASE)
_NEGATIVE_HINT_RE = re.compile(
    r"(忙|累|困|睡|病|难受|烦|崩溃|不行|不想|加班|委屈|哭|生气|stressed|tired|busy|sick|upset)"
)
_POSITIVE_HINT_RE = re.compile(r"(哈哈|开心|好耶|太棒|稳了|nice|great|awesome|congrats|good job)", re.IGNORECASE)
_CASUAL_DEFAULT_EMOJI_NAMES = ["微笑", "旺柴", "机智", "捂脸", "嘿哈", "呲牙", "让我看看", "耶"]


def _same_language_hint(text: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", text or ""):
        return "Reply in Chinese."
    return "Reply in English."


def _normalize_reply_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = _LEADING_META_RE.sub("", value).strip()
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[0]
    line = line.strip("\"'“”‘’")
    line = _SENTENCE_FINAL_PERIOD_RE.sub("", line).strip()
    return line


def _contains_emoji(text: str) -> bool:
    value = str(text or "")
    return bool(_WECHAT_CODE_RE.search(value) or _UNICODE_EMOJI_RE.search(value))


def _preferred_emoji_names(inbound_text: str) -> list[str]:
    inbound = str(inbound_text or "").strip()
    names: list[str] = []
    if _QUESTION_HINT_RE.search(inbound):
        names.extend(["让我看看", "机智", "嘿哈", "捂脸"])
    if _NEGATIVE_HINT_RE.search(inbound):
        names.extend(["捂脸", "叹气", "拥抱", "加油", "流泪"])
    if _POSITIVE_HINT_RE.search(inbound):
        names.extend(["呲牙", "嘿哈", "耶", "鼓掌", "旺柴"])
    if not names:
        names.extend(_CASUAL_DEFAULT_EMOJI_NAMES)
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        deduped.append(name)
    return deduped


def _stable_pick_codes(
    contact: str,
    inbound_text: str,
    available_codes: dict[str, str],
    preferred_names: list[str],
    count: int,
) -> list[str]:
    pool_names = [name for name in preferred_names if name in available_codes]
    if not pool_names:
        pool_names = [name for name in _CASUAL_DEFAULT_EMOJI_NAMES if name in available_codes]
    if not pool_names:
        pool_names = list(available_codes.keys())
    if not pool_names:
        return []
    seed = f"{contact}\0{inbound_text}".encode("utf-8")
    start = int(hashlib.sha1(seed).hexdigest(), 16) % len(pool_names)
    ordered = pool_names[start:] + pool_names[:start]
    selected: list[str] = []
    for name in ordered:
        code = available_codes.get(name)
        if not code or code in selected:
            continue
        selected.append(code)
        if len(selected) >= count:
            break
    return selected


def _append_code_with_budget(base_text: str, code: str, max_chars: int) -> str:
    if not code:
        return base_text
    if not base_text:
        return code[:max_chars].strip()
    candidate = f"{base_text} {code}".strip()
    if len(candidate) <= max_chars:
        return candidate
    budget = max_chars - len(code) - 1
    if budget <= 0:
        return base_text[:max_chars].strip()
    trimmed = base_text[:budget].rstrip()
    trimmed = _SENTENCE_FINAL_PERIOD_RE.sub("", trimmed).rstrip()
    return f"{trimmed} {code}".strip()


def _clean_context_line(text: str, max_chars: int = 180) -> str:
    value = " ".join(str(text or "").strip().split())
    if not value:
        return ""
    return value[:max_chars].strip()


def _format_context_block(conversation_context: list[dict[str, str]] | None) -> str:
    if not conversation_context:
        return ""
    lines: list[str] = []
    for item in conversation_context:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        text = _clean_context_line(str(item.get("text", "")))
        if not text:
            continue
        speaker = "Me" if role in {"self", "me", "outbound", "assistant"} else "Them"
        lines.append(f"{speaker}: {text}")
    if not lines:
        return ""
    return "Recent chat context (oldest to latest):\n" + "\n".join(lines[-8:]) + "\n\n"


def _format_contact_memory_block(contact_memory: dict[str, Any] | None) -> str:
    if not isinstance(contact_memory, dict):
        return ""
    profile = _clean_context_line(str(contact_memory.get("profile", "")), max_chars=160)
    recent_summary = _clean_context_line(str(contact_memory.get("recent_summary", "")), max_chars=320)
    lines: list[str] = []
    if profile:
        lines.append(f"Contact profile: {profile}")
    if recent_summary:
        lines.append(f"Longer-term memory with this contact: {recent_summary}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


class OllamaClient:
    def __init__(
        self,
        url: str,
        model: str,
        max_reply_chars: int = 90,
        style_instructions: str = "",
        emoji_pack_zip_path: str = "",
        emoji_enabled: bool = True,
        emoji_min_count: int = 0,
        emoji_max_count: int = 1,
    ) -> None:
        self.url = url
        self.model = model
        self.max_reply_chars = max_reply_chars
        self.style_instructions = str(style_instructions or "").strip()
        self.emoji_enabled = bool(emoji_enabled)
        self.emoji_min_count = max(0, int(emoji_min_count))
        self.emoji_max_count = max(self.emoji_min_count, int(emoji_max_count))
        self.emoji_names = load_wechat_emoji_names(str(emoji_pack_zip_path or "").strip())
        self.emoji_codes = build_wechat_emoji_codes(self.emoji_names)

    def generate_reply(
        self,
        contact: str,
        inbound_text: str,
        conversation_context: list[dict[str, str]] | None = None,
        contact_memory: dict[str, Any] | None = None,
        screenshot_path: str | None = None,
    ) -> str:
        del screenshot_path
        num_predict = max(12, min(48, self.max_reply_chars // 2))
        style_block = f"{self.style_instructions}\n" if self.style_instructions else ""
        emoji_prompt_block = ""
        context_block = _format_context_block(conversation_context)
        memory_block = _format_contact_memory_block(contact_memory)
        if self.emoji_enabled and self.emoji_codes:
            sampled = " ".join(self.emoji_codes[:20])
            emoji_prompt_block = (
                "Use at most 1 WeChat emoji code in square brackets, and only when it feels natural.\n"
                "Most replies should have no emoji.\n"
                f"Use only codes from this list: {sampled}\n"
                "Do not invent new emoji codes.\n"
            )
        prompt = (
            "You write short, natural WeChat replies.\n"
            f"{_same_language_hint(inbound_text)}\n"
            f"{style_block}"
            f"{emoji_prompt_block}"
            "Keep it colloquial and human.\n"
            "Do not explain yourself.\n"
            "Do not use bullet points.\n"
            "Do not use quotes around the reply.\n"
            "Do not end the reply with a period.\n"
            f"Stay under {self.max_reply_chars} characters.\n\n"
            f"Contact: {contact}\n"
            f"{memory_block}"
            f"{context_block}"
            f"Latest incoming message: {inbound_text}\n\n"
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
        text = _normalize_reply_text(text)
        if not text:
            raise RuntimeError("ollama returned an empty reply")
        if self.emoji_enabled and self.emoji_codes and not _contains_emoji(text) and self.emoji_min_count > 0:
            available_codes = {
                code[1:-1]: code
                for code in self.emoji_codes
                if len(code) >= 3 and code.startswith("[") and code.endswith("]")
            }
            preferred_names = _preferred_emoji_names(inbound_text)
            count = max(0, self.emoji_min_count)
            if self.emoji_max_count > count and len(text) <= 24 and count > 0:
                count += 1
            count = min(count, max(0, self.emoji_max_count))
            for code in _stable_pick_codes(contact, inbound_text, available_codes, preferred_names, count):
                text = _append_code_with_budget(text, code, self.max_reply_chars)
        text = _normalize_reply_text(text[: self.max_reply_chars].strip())
        if not text:
            raise RuntimeError("ollama returned an empty reply")
        return text
