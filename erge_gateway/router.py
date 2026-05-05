from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from erge_gateway.cache import JsonFileCache
from erge_gateway.clients.logic_client import LogicClient
from erge_gateway.clients.vision_client import VisionClient
from erge_gateway.config import Settings
from erge_gateway.preprocess.docx_ingest import ingest_docx
from erge_gateway.preprocess.pdf_ingest import ingest_pdf
from erge_gateway.preprocess.xlsx_ingest import ingest_xlsx
from erge_gateway.schemas import (
    AttachmentPayload,
    ChatCompletionRequest,
    ChatCompletionResponse,
    IngestResult,
    ReasoningInput,
    TraceRecord,
    VisionSummary,
)


class GatewayRouter:
    _INLINE_FILE_REF_RE = re.compile(r"\[file attached:\s*(file://[^\]\s]+)\]", re.IGNORECASE)

    def __init__(self, settings: Settings, cache: JsonFileCache, vision_client: VisionClient, logic_client: LogicClient) -> None:
        self.settings = settings
        self.cache = cache
        self.vision_client = vision_client
        self.logic_client = logic_client

    def _write_trace(self, trace: TraceRecord) -> None:
        path = self.settings.runs_root / f"{trace.request_id}.json"
        path.write_text(json.dumps(trace.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_data_url(self, url: str, request_id: str, index: int) -> Path:
        header, encoded = url.split(",", 1)
        ext = "png"
        if ";base64" not in header:
            raise ValueError("unsupported data URL")
        if "image/jpeg" in header:
            ext = "jpg"
        elif "image/webp" in header:
            ext = "webp"
        path = self.settings.tmp_root / f"{request_id}-{index}.{ext}"
        path.write_bytes(base64.b64decode(encoded))
        return path

    def _extract_from_message(self, request: ChatCompletionRequest, request_id: str) -> tuple[str, list[AttachmentPayload]]:
        question_parts: list[str] = []
        attachments: list[AttachmentPayload] = []
        last_user = next((msg for msg in reversed(request.messages) if msg.role == "user"), request.messages[-1])
        content = last_user.content
        if isinstance(content, str):
            return self._extract_inline_file_refs(content.strip(), attachments), attachments
        for idx, part in enumerate(content or []):
            kind = getattr(part, "type", "") or ""
            if kind == "text":
                text = str(part.text or "").strip()
                if text:
                    question_parts.append(text)
                continue
            if kind in {"image_url", "input_image", "file_url", "input_file", "file"}:
                raw = None
                if kind in {"image_url", "input_image"}:
                    raw = part.image_url
                elif kind == "file_url":
                    raw = part.file_url
                else:
                    raw = part.file
                if isinstance(raw, dict):
                    url = str(
                        raw.get("url")
                        or raw.get("file_url")
                        or raw.get("image_url")
                        or raw.get("path")
                        or ""
                    ).strip()
                else:
                    url = str(raw or "").strip()
                if not url:
                    continue
                if url.startswith("file://"):
                    path = Path(unquote(url[len("file://") :]))
                elif url.startswith("data:"):
                    path = self._save_data_url(url, request_id, idx)
                else:
                    path = Path(url)
                ext = path.suffix.lower()
                kind_label = "image"
                if ext == ".pdf":
                    kind_label = "pdf"
                elif ext == ".docx":
                    kind_label = "docx"
                elif ext == ".xlsx":
                    kind_label = "xlsx"
                attachments.append(
                    AttachmentPayload(
                        kind=kind_label,
                        source_path=str(path),
                        file_name=path.name,
                        mime_type=None,
                        size_bytes=path.stat().st_size if path.exists() else None,
                    )
                )
        question = "\n".join(question_parts).strip()
        return self._extract_inline_file_refs(question, attachments), attachments

    def _extract_inline_file_refs(self, text: str, attachments: list[AttachmentPayload]) -> str:
        if not text:
            return text
        for match in self._INLINE_FILE_REF_RE.finditer(text):
            url = str(match.group(1) or "").strip()
            if not url.startswith("file://"):
                continue
            path = Path(unquote(url[len("file://") :]))
            ext = path.suffix.lower()
            kind_label = "image"
            if ext == ".pdf":
                kind_label = "pdf"
            elif ext == ".docx":
                kind_label = "docx"
            elif ext == ".xlsx":
                kind_label = "xlsx"
            attachments.append(
                AttachmentPayload(
                    kind=kind_label,
                    source_path=str(path),
                    file_name=path.name,
                    mime_type=None,
                    size_bytes=path.stat().st_size if path.exists() else None,
                )
            )
        cleaned = self._INLINE_FILE_REF_RE.sub("", text)
        return cleaned.strip()

    def _ingest_attachment(self, attachment: AttachmentPayload, trace: TraceRecord) -> IngestResult:
        path = Path(attachment.source_path or "")
        if attachment.kind == "image":
            trace.vision_started = True
            summary, cache_hit = self.vision_client.summarize_image(path)
            trace.vision_finished = True
            trace.vision_cache_hit = cache_hit
            trace.ocr_used = bool(summary.visible_text)
            trace.vision_used = True
            trace.vision_backend_actual = self.settings.vision_model
            return IngestResult(
                kind="image",
                source_path=str(path),
                image_paths=[str(path)],
                text_chunks=[],
                metadata={"ocr_used": trace.ocr_used, "extraction_mode": "image_direct"},
                vision_summary=summary,
                routing_hint="image_to_logic",
            )
        if attachment.kind == "pdf":
            result = ingest_pdf(path, self.settings)
            trace.ingest_mode = str(result.metadata.get("extraction_mode") or "mixed")
            if result.metadata.get("extraction_mode") == "ocr_scan" and result.image_paths:
                result = self._augment_scan_pdf(result, trace)
            return result
        if attachment.kind == "docx":
            return ingest_docx(path)
        if attachment.kind == "xlsx":
            return ingest_xlsx(path)
        return IngestResult(kind="text", source_path=str(path), text_chunks=[])

    def _augment_scan_pdf(self, ingest: IngestResult, trace: TraceRecord) -> IngestResult:
        page_summaries: list[VisionSummary] = []
        cache_hits: list[bool] = []
        trace.vision_started = True
        for page_path in ingest.image_paths:
            summary, cache_hit = self.vision_client.summarize_image(Path(page_path))
            page_summaries.append(summary)
            cache_hits.append(cache_hit)
        trace.vision_finished = True
        trace.vision_cache_hit = bool(cache_hits) and all(cache_hits)
        trace.ocr_used = any(bool(item.visible_text) for item in page_summaries)
        trace.vision_used = True
        trace.vision_backend_actual = self.settings.vision_model

        combined_summary = "\n".join(
            f"Page {index}: {item.summary}".strip()
            for index, item in enumerate(page_summaries, start=1)
            if item.summary
        ).strip()
        combined_visible_text = "\n\n".join(
            f"[Page {index}]\n{item.visible_text}".strip()
            for index, item in enumerate(page_summaries, start=1)
            if item.visible_text
        ).strip()
        combined_entities: list[str] = []
        combined_uncertainties: list[str] = []
        extra_chunks: list[str] = []
        for index, item in enumerate(page_summaries, start=1):
            if item.visible_text:
                extra_chunks.append(f"Page {index} OCR:\n{item.visible_text}")
            if item.summary:
                extra_chunks.append(f"Page {index} visual summary:\n{item.summary}")
            combined_entities.extend(item.entities)
            combined_uncertainties.extend(item.uncertainties)

        metadata = dict(ingest.metadata)
        metadata["ocr_used"] = bool(combined_visible_text)
        metadata["vision_page_count"] = len(page_summaries)
        return ingest.model_copy(
            update={
                "text_chunks": [*ingest.text_chunks, *extra_chunks],
                "metadata": metadata,
                "vision_summary": VisionSummary(
                    summary=combined_summary,
                    visible_text=combined_visible_text,
                    entities=combined_entities,
                    uncertainties=combined_uncertainties,
                ),
                "routing_hint": "pdf_scan_to_logic",
            }
        )

    @staticmethod
    def _looks_chinese(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")

    def _reasoning_messages(self, reasoning_input: ReasoningInput) -> list[dict[str, str]]:
        ingest = reasoning_input.ingest
        wants_chinese = self._looks_chinese(reasoning_input.user_question)
        if wants_chinese:
            sections = [
                "你是 Erge Brother 的最终推理后端。",
                "请直接回答用户问题，不要复述整段视觉摘要。",
                "回答必须使用中文，并优先保持自然、简洁、像正常聊天。",
                "如果证据不足，就明确说不确定，不要瞎猜。",
                f"用户问题：\n{reasoning_input.user_question or '(无)'}",
                f"输入类型：{ingest.kind}",
            ]
        else:
            sections = [
                "You are the final reasoning backend for Erge Brother.",
                "Answer the user's question directly in natural language.",
                "Reply in the same language as the user's question.",
                "Do not merely restate the visual summary; answer the question itself.",
                "Use the provided evidence. If evidence is uncertain, say so briefly.",
                f"Question:\n{reasoning_input.user_question or '(none)'}",
                f"Input kind: {ingest.kind}",
            ]
        if ingest.vision_summary:
            if wants_chinese:
                sections.append(f"可见文字：\n{ingest.vision_summary.visible_text or '(无)'}")
                sections.append(f"视觉摘要：\n{ingest.vision_summary.summary or '(无)'}")
            else:
                sections.append(f"Visible text:\n{ingest.vision_summary.visible_text or '(none)'}")
                sections.append(f"Visual summary:\n{ingest.vision_summary.summary or '(none)'}")
            if ingest.vision_summary.entities:
                label = "识别到的元素" if wants_chinese else "Entities"
                sections.append(f"{label}:\n- " + "\n- ".join(ingest.vision_summary.entities))
            if ingest.vision_summary.uncertainties:
                label = "不确定点" if wants_chinese else "Uncertainties"
                sections.append(f"{label}:\n- " + "\n- ".join(ingest.vision_summary.uncertainties))
        if ingest.text_chunks:
            label = "提取文本" if wants_chinese else "Extracted text"
            sections.append(f"{label}:\n" + "\n\n".join(ingest.text_chunks[:8]))
        if ingest.metadata:
            label = "元数据" if wants_chinese else "Metadata"
            sections.append(f"{label}:\n" + json.dumps(ingest.metadata, ensure_ascii=False, indent=2))
        prompt = "\n\n".join(sections)
        system_text = (
            "请直接用自然中文回答，不要输出 JSON、流水线细节或英文模板。"
            if wants_chinese
            else "Answer in plain natural language. Match the user's language. Do not output JSON or internal pipeline details."
        )
        return [
            {"role": "system", "content": system_text},
            {"role": "user", "content": prompt},
        ]

    def handle_chat_completion(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        request_id = uuid.uuid4().hex
        trace = TraceRecord(request_id=request_id, received_at=datetime.now(timezone.utc).isoformat())
        question, attachments = self._extract_from_message(request, request_id)
        trace.attachment_received = bool(attachments)
        trace.attachments_detected = len(attachments)
        trace.attachment_types = [item.kind for item in attachments]
        trace.source_paths = [item.source_path or "" for item in attachments]
        trace.message_kind = attachments[0].kind if attachments else "text"

        health = self.logic_client.probe_primary()
        trace.logic_probe_latency_ms = health.latency_ms
        trace.status = health.status
        trace.logic_backend_primary = self.settings.logic_primary_model
        use_primary = health.status == "healthy"
        trace.logic_backend_actual = "pc_5080" if use_primary else "local_small"
        trace.role_actual = "middle" if use_primary else "small"
        if not use_primary:
            trace.fallback_triggered = True
            trace.fallback_reason = health.reason
            trace.final_mode = "fallback_to_small"

        if attachments:
            trace.attachment_classified = True
            ingest = self._ingest_attachment(attachments[0], trace)
            if not use_primary:
                fallback_question = question or "用户发来了一条包含附件的消息。"
                fallback_prompt = (
                    "当前 5080 逻辑后端不可用，系统已回退到本地稳定版。"
                    "请只根据用户文字自然回答；如果用户依赖图片内容，就简短说明当前无法查看图片。"
                )
                messages = [
                    {"role": "system", "content": fallback_prompt},
                    {"role": "user", "content": fallback_question},
                ]
                trace.logic_started = True
                result = self.logic_client.chat(messages, prefer_primary=False)
                trace.logic_finished = True
            else:
                reasoning = ReasoningInput(user_question=question, ingest=ingest)
                trace.final_mode = {
                    "image": "image_to_vision_to_logic",
                    "pdf": "pdf_text_to_logic" if ingest.metadata.get("extraction_mode") == "text_native" else "pdf_scan_to_vision_to_logic",
                    "docx": "docx_to_logic",
                    "xlsx": "xlsx_to_logic",
                }.get(ingest.kind, "text_to_logic")
                trace.logic_started = True
                result = self.logic_client.chat(self._reasoning_messages(reasoning), prefer_primary=True)
                trace.logic_finished = True
        else:
            trace.final_mode = "text_to_logic" if use_primary else "fallback_to_small"
            trace.logic_started = True
            result = self.logic_client.chat([msg.model_dump(exclude_none=True) for msg in request.messages], prefer_primary=use_primary)
            trace.logic_finished = True

        if result.backend != trace.logic_backend_actual:
            trace.logic_backend_actual = result.backend
            trace.role_actual = "small" if result.backend == "local_small" else "middle"
            trace.final_mode = "fallback_to_small" if result.backend == "local_small" else trace.final_mode
        if result.fallback_reason:
            trace.fallback_triggered = True
            trace.fallback_reason = result.fallback_reason

        self._write_trace(trace)
        return ChatCompletionResponse(
            id=f"chatcmpl-{request_id}",
            created=int(datetime.now(timezone.utc).timestamp()),
            model=request.model,
            choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": result.content}}],
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
