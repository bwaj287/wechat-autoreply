from __future__ import annotations

import hashlib
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from .ollama_client import (
    OllamaClient,
    _append_code_with_budget,
    _contains_emoji,
    _format_contact_memory_block,
    _format_context_block,
    _format_quoted_message_block,
    _normalize_reply_text,
    _preferred_emoji_names,
    _same_language_hint,
    _stable_pick_codes,
)


_PHOTO_PLACEHOLDER_RE = re.compile(
    r"(?i)(photo|picture|image|pic|图片|照片|相片|截图)"
)


class ErgeClient:
    def __init__(
        self,
        gateway_url: str,
        health_url: str,
        model: str,
        fallback_client: OllamaClient,
        health_timeout_seconds: float = 2,
        health_cache_seconds: float = 15,
        request_timeout_seconds: float = 120,
    ) -> None:
        self.gateway_url = str(gateway_url or "").strip()
        self.health_url = str(health_url or "").strip()
        self.model = str(model or "brother").strip() or "brother"
        self.fallback_client = fallback_client
        self.health_timeout_seconds = max(0.5, float(health_timeout_seconds or 2))
        self.health_cache_seconds = max(0.0, float(health_cache_seconds or 0))
        self.request_timeout_seconds = max(5.0, float(request_timeout_seconds or 120))
        self.last_backend = "local_small"
        self.last_reason = "init"
        self._cached_health_until = 0.0
        self._cached_health_ok = False

    def _erge_healthy(self) -> bool:
        now = time.monotonic()
        if now < self._cached_health_until:
            return self._cached_health_ok
        try:
            response = requests.get(self.health_url, timeout=self.health_timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            probe = payload.get("logic_probe") or {}
            self._cached_health_ok = str(probe.get("status") or "").strip().lower() == "healthy"
            self._cached_health_until = now + self.health_cache_seconds
            return self._cached_health_ok
        except Exception as exc:
            self._cached_health_ok = False
            self._cached_health_until = now + min(self.health_cache_seconds, 5.0)
            self.last_backend = "local_small"
            self.last_reason = f"erge_health_error:{exc}"
            return False

    def _build_prompt(
        self,
        contact: str,
        inbound_text: str,
        conversation_context: list[dict[str, str]] | None = None,
        contact_memory: dict[str, Any] | None = None,
        quoted_message: dict[str, Any] | None = None,
    ) -> str:
        style_block = (
            f"{self.fallback_client.style_instructions}\n"
            if self.fallback_client.style_instructions
            else ""
        )
        emoji_prompt_block = ""
        context_block = _format_context_block(conversation_context)
        memory_block = _format_contact_memory_block(contact_memory)
        quoted_block = _format_quoted_message_block(quoted_message)
        if self.fallback_client.emoji_enabled and self.fallback_client.emoji_codes:
            sampled = " ".join(self.fallback_client.emoji_codes[:20])
            emoji_prompt_block = (
                "Use at most 1 WeChat emoji code in square brackets, and only when it feels natural.\n"
                "Most replies should have no emoji.\n"
                f"Use only codes from this list: {sampled}\n"
                "Do not invent new emoji codes.\n"
            )
        screenshot_hint = (
            "The latest incoming message may be a photo/sticker-only message. "
            "If a screenshot is attached, focus on the newest left-side incoming media bubble, "
            "and infer the reply from that image content instead of the literal placeholder text. "
            "If the image content is unclear, reply conservatively and do not hallucinate.\n"
            if self._looks_like_photo_placeholder(inbound_text)
            else "If a screenshot is attached, use it only as supporting evidence for the current WeChat chat.\n"
        )
        return (
            "You write short, natural WeChat replies.\n"
            f"{_same_language_hint(inbound_text)}\n"
            f"{style_block}"
            f"{emoji_prompt_block}"
            f"{screenshot_hint}"
            "Focus on the latest incoming message and recent context.\n"
            "If the latest message is replying to a quoted earlier line, respond mainly to the new reply and use the quote only as context.\n"
            "Keep it colloquial and human.\n"
            "Do not explain yourself.\n"
            "Do not use bullet points.\n"
            "Do not use quotes around the reply.\n"
            "Do not end the reply with a period.\n"
            f"Stay under {self.fallback_client.max_reply_chars} characters.\n\n"
            f"Contact: {contact}\n"
            f"{memory_block}"
            f"{context_block}"
            f"{quoted_block}"
            f"Latest incoming message: {inbound_text}\n\n"
            "Reply with the final WeChat message only."
        )

    @staticmethod
    def _looks_like_photo_placeholder(text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return False
        compact = (
            value.replace("［", "[")
            .replace("］", "]")
            .replace("（", "(")
            .replace("）", ")")
            .strip()
        )
        if _PHOTO_PLACEHOLDER_RE.search(compact):
            return True
        return compact.startswith("[") and compact.endswith("]") and len(compact) <= 24

    @staticmethod
    def _background_rgb(image: Image.Image) -> tuple[int, int, int]:
        rgb = image.convert("RGB")
        width, height = rgb.size
        corners = [
            rgb.getpixel((0, 0)),
            rgb.getpixel((max(0, width - 1), 0)),
            rgb.getpixel((0, max(0, height - 1))),
            rgb.getpixel((max(0, width - 1), max(0, height - 1))),
        ]
        return tuple(int(sum(px[idx] for px in corners) / len(corners)) for idx in range(3))

    @staticmethod
    def _likely_media_component_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
        width, height = image.size
        if width < 120 or height < 120:
            return None
        sample_w = max(96, min(320, width // 2))
        sample_h = max(96, min(480, height // 2))
        sample = image.convert("RGB").resize((sample_w, sample_h), Image.Resampling.BILINEAR)
        bg_r, bg_g, bg_b = ErgeClient._background_rgb(sample)

        x_limit = int(sample_w * 0.88)
        y_start = int(sample_h * 0.12)
        y_end = int(sample_h * 0.93)
        visited: set[tuple[int, int]] = set()
        largest_area = 0
        largest_bbox: tuple[int, int, int, int] | None = None

        def is_foreground(pixel: tuple[int, int, int]) -> bool:
            r, g, b = pixel
            brightness = max(r, g, b)
            bg_brightness = max(bg_r, bg_g, bg_b)
            delta = abs(r - bg_r) + abs(g - bg_g) + abs(b - bg_b)
            return brightness >= max(72, bg_brightness + 24) or delta >= 90

        for y in range(y_start, y_end):
            for x in range(0, x_limit):
                if (x, y) in visited:
                    continue
                visited.add((x, y))
                if not is_foreground(sample.getpixel((x, y))):
                    continue
                queue = deque([(x, y)])
                area = 0
                min_x = max_x = x
                min_y = max_y = y
                while queue:
                    cx, cy = queue.popleft()
                    area += 1
                    min_x = min(min_x, cx)
                    max_x = max(max_x, cx)
                    min_y = min(min_y, cy)
                    max_y = max(max_y, cy)
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if nx < 0 or ny < y_start or nx >= x_limit or ny >= y_end:
                            continue
                        if (nx, ny) in visited:
                            continue
                        visited.add((nx, ny))
                        if is_foreground(sample.getpixel((nx, ny))):
                            queue.append((nx, ny))
                box_w = max_x - min_x + 1
                box_h = max_y - min_y + 1
                if area < int(sample_w * sample_h * 0.015):
                    continue
                if box_w < int(sample_w * 0.14) or box_h < int(sample_h * 0.12):
                    continue
                if area > largest_area:
                    largest_area = area
                    largest_bbox = (min_x, min_y, max_x + 1, max_y + 1)

        if not largest_bbox:
            return None
        sx0, sy0, sx1, sy1 = largest_bbox
        scale_x = width / sample_w
        scale_y = height / sample_h
        pad_x = max(8, int((sx1 - sx0) * 0.08))
        pad_y = max(8, int((sy1 - sy0) * 0.08))
        ox0 = max(0, int(sx0 * scale_x) - pad_x)
        oy0 = max(0, int(sy0 * scale_y) - pad_y)
        ox1 = min(width, int(sx1 * scale_x) + pad_x)
        oy1 = min(height, int(sy1 * scale_y) + pad_y)
        if ox1 - ox0 < 80 or oy1 - oy0 < 80:
            return None
        return (ox0, oy0, ox1, oy1)

    @staticmethod
    def _upscale_image_if_needed(image: Image.Image, *, prefer_zoom: bool) -> Image.Image:
        width, height = image.size
        longest = max(width, height)
        if not prefer_zoom and longest >= 1200:
            return image
        target_longest = 1700 if prefer_zoom else 1200
        if longest >= target_longest:
            return image
        scale = min(3.0, target_longest / max(1, longest))
        if scale <= 1.05:
            return image
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        return image.resize(new_size, Image.Resampling.LANCZOS)

    @staticmethod
    def _save_processed_focus_image(
        screenshot: Path,
        image: Image.Image,
        *,
        suffix: str,
        digest_key: str,
    ) -> Path:
        digest = hashlib.sha1(digest_key.encode("utf-8")).hexdigest()[:12]
        dest = screenshot.with_name(f"{screenshot.stem}-{suffix}-{digest}.png")
        image.save(dest, format="PNG")
        return dest

    @staticmethod
    def _build_visual_focus_image(screenshot: Path, inbound_text: str) -> Path:
        if not screenshot.exists() or not screenshot.is_file():
            return screenshot
        if not ErgeClient._looks_like_photo_placeholder(inbound_text):
            try:
                with Image.open(screenshot) as source:
                    width, height = source.size
                    if width < 320 or height < 240:
                        return screenshot
                    # Roster captures include the contact list on the left; crop to the
                    # right chat panel before sending visual context to erge so that left
                    # sidebar previews from other contacts don't contaminate the reply.
                    if "wechat-roster-" in screenshot.name:
                        left = int(width * 0.33)
                        top = int(height * 0.02)
                        right = int(width * 0.99)
                        bottom = int(height * 0.98)
                        if right - left < 160 or bottom - top < 160:
                            return screenshot
                        cropped = source.crop((left, top, right, bottom))
                        media_bbox = ErgeClient._likely_media_component_bbox(cropped)
                        if media_bbox:
                            focused = cropped.crop(media_bbox)
                            focused = ErgeClient._upscale_image_if_needed(focused, prefer_zoom=True)
                            return ErgeClient._save_processed_focus_image(
                                screenshot,
                                focused,
                                suffix="vision-media-focus",
                                digest_key=(
                                    f"{screenshot}:{screenshot.stat().st_mtime_ns}:{left}:{top}:{right}:{bottom}:"
                                    f"{media_bbox}:media"
                                ),
                            )
                        cropped = ErgeClient._upscale_image_if_needed(cropped, prefer_zoom=False)
                        return ErgeClient._save_processed_focus_image(
                            screenshot,
                            cropped,
                            suffix="chat-focus",
                            digest_key=(
                                f"{screenshot}:{screenshot.stat().st_mtime_ns}:{left}:{top}:{right}:{bottom}:chat-only"
                            ),
                        )
            except Exception:
                return screenshot
            return screenshot
        try:
            with Image.open(screenshot) as source:
                width, height = source.size
                if width < 320 or height < 240:
                    return screenshot
                is_chat_only = "wechat-chat-" in screenshot.name
                if is_chat_only:
                    left = int(width * 0.04)
                    top = int(height * 0.24)
                    right = int(width * 0.72)
                    bottom = int(height * 0.92)
                else:
                    left = int(width * 0.36)
                    top = int(height * 0.22)
                    right = int(width * 0.84)
                    bottom = int(height * 0.92)
                if right - left < 120 or bottom - top < 120:
                    return screenshot
                cropped = source.crop((left, top, right, bottom))
                media_bbox = ErgeClient._likely_media_component_bbox(cropped)
                focused = cropped.crop(media_bbox) if media_bbox else cropped
                focused = ErgeClient._upscale_image_if_needed(focused, prefer_zoom=True)
                return ErgeClient._save_processed_focus_image(
                    screenshot,
                    focused,
                    suffix="vision-focus",
                    digest_key=(
                        f"{screenshot}:{screenshot.stat().st_mtime_ns}:{left}:{top}:{right}:{bottom}:{media_bbox}"
                    ),
                )
        except Exception:
            return screenshot
        return screenshot

    def _postprocess(self, contact: str, inbound_text: str, raw_text: str) -> str:
        text = _normalize_reply_text(raw_text)
        if not text:
            raise RuntimeError("erge returned an empty reply")
        if (
            self.fallback_client.emoji_enabled
            and self.fallback_client.emoji_codes
            and not _contains_emoji(text)
            and self.fallback_client.emoji_min_count > 0
        ):
            available_codes = {
                code[1:-1]: code
                for code in self.fallback_client.emoji_codes
                if len(code) >= 3 and code.startswith("[") and code.endswith("]")
            }
            preferred_names = _preferred_emoji_names(inbound_text)
            count = max(0, self.fallback_client.emoji_min_count)
            if self.fallback_client.emoji_max_count > count and len(text) <= 24 and count > 0:
                count += 1
            count = min(count, max(0, self.fallback_client.emoji_max_count))
            for code in _stable_pick_codes(contact, inbound_text, available_codes, preferred_names, count):
                text = _append_code_with_budget(text, code, self.fallback_client.max_reply_chars)
        text = _normalize_reply_text(text[: self.fallback_client.max_reply_chars].strip())
        if not text:
            raise RuntimeError("erge returned an empty reply")
        return text

    def generate_reply(
        self,
        contact: str,
        inbound_text: str,
        conversation_context: list[dict[str, str]] | None = None,
        contact_memory: dict[str, Any] | None = None,
        screenshot_path: str | None = None,
        quoted_message: dict[str, Any] | None = None,
    ) -> str:
        if not self._erge_healthy():
            self.last_backend = "local_small"
            if not self.last_reason:
                self.last_reason = "erge_unhealthy"
            return self.fallback_client.generate_reply(
                contact,
                inbound_text,
                conversation_context,
                contact_memory=contact_memory,
                quoted_message=quoted_message,
            )

        prompt = self._build_prompt(
            contact,
            inbound_text,
            conversation_context,
            contact_memory,
            quoted_message=quoted_message,
        )
        user_content: str | list[dict[str, Any]]
        screenshot = Path(str(screenshot_path or "").strip())
        visual_path = self._build_visual_focus_image(screenshot, inbound_text)
        if visual_path.exists() and visual_path.is_file():
            user_content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": visual_path.resolve().as_uri()}},
            ]
        else:
            user_content = prompt

        try:
            response = requests.post(
                self.gateway_url,
                json={
                    "model": self.model,
                    "stream": False,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Return only the final WeChat reply in plain language. Do not output JSON or pipeline details.",
                        },
                        {"role": "user", "content": user_content},
                    ],
                },
                timeout=self.request_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            choices = payload.get("choices") or []
            message = choices[0].get("message", {}) if choices else {}
            content = str(message.get("content") or "").strip()
            self.last_backend = "erge_brother"
            self.last_reason = "erge_healthy"
            return self._postprocess(contact, inbound_text, content)
        except Exception as exc:
            self.last_backend = "local_small"
            self.last_reason = f"erge_request_error:{exc}"
            return self.fallback_client.generate_reply(
                contact,
                inbound_text,
                conversation_context,
                contact_memory=contact_memory,
                quoted_message=quoted_message,
            )
