from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatContentPart(BaseModel):
    type: str
    text: str | None = None
    image_url: Any | None = None
    file_url: Any | None = None
    file: Any | None = None
    model_config = ConfigDict(extra="allow")


class ChatMessage(BaseModel):
    role: str
    content: str | list[ChatContentPart] | None = None
    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, alias="max_tokens")
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class AttachmentPayload(BaseModel):
    kind: Literal["text", "image", "pdf", "docx", "xlsx"]
    source_path: str | None = None
    mime_type: str | None = None
    file_name: str | None = None
    size_bytes: int | None = None
    text_inline: str = ""
    image_paths: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VisionSummary(BaseModel):
    summary: str = ""
    visible_text: str = ""
    entities: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class IngestResult(BaseModel):
    kind: Literal["text", "image", "pdf", "docx", "xlsx"]
    source_path: str | None = None
    text_chunks: list[str] = Field(default_factory=list)
    image_paths: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    vision_summary: VisionSummary | None = None
    routing_hint: str | None = None


class ReasoningInput(BaseModel):
    user_question: str
    ingest: IngestResult


class TraceRecord(BaseModel):
    request_id: str
    received_at: str
    role_target: str = "middle"
    role_actual: str = "middle"
    message_kind: str = "text"
    attachments_detected: int = 0
    attachment_types: list[str] = Field(default_factory=list)
    source_paths: list[str] = Field(default_factory=list)
    ocr_used: bool = False
    vision_used: bool = False
    ingest_mode: str = "text_native"
    vision_cache_hit: bool = False
    ingest_cache_hit: bool = False
    logic_backend_primary: str = ""
    logic_backend_actual: str = ""
    vision_backend_actual: str = ""
    logic_probe_latency_ms: int | None = None
    status: str = "unknown"
    fallback_triggered: bool = False
    fallback_reason: str | None = None
    attachment_received: bool = False
    attachment_classified: bool = False
    vision_started: bool = False
    vision_finished: bool = False
    logic_started: bool = False
    logic_finished: bool = False
    final_mode: str = "text_to_logic"
    error_summary: str | None = None
    model_config = ConfigDict(extra="allow")


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: dict[str, int] = Field(default_factory=dict)
