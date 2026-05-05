from __future__ import annotations

import json
from collections.abc import Iterator

from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from erge_gateway.cache import JsonFileCache
from erge_gateway.clients.logic_client import LogicClient
from erge_gateway.clients.vision_client import VisionClient
from erge_gateway.config import load_settings
from erge_gateway.router import GatewayRouter
from erge_gateway.schemas import ChatCompletionRequest, ChatCompletionResponse

settings = load_settings()
cache = JsonFileCache(settings.cache_root)
vision_client = VisionClient(settings, cache)
logic_client = LogicClient(settings)
router = GatewayRouter(settings, cache, vision_client, logic_client)

app = FastAPI(title="Erge Gateway", version="0.1.0")


def _chunk_text(text: str, *, chunk_size: int = 160) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _iter_openai_stream(response: ChatCompletionResponse) -> Iterator[str]:
    created = response.created
    model = response.model
    response_id = response.id
    content = str(response.choices[0]["message"].get("content", "") or "")

    first_chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

    for piece in _chunk_text(content):
        if not piece:
            continue
        chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    final_chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/health")
def health() -> dict:
    probe = logic_client.probe_primary()
    return {
        "service": "erge_gateway",
        "status": "ok",
        "logic_probe": {
            "status": probe.status,
            "latency_ms": probe.latency_ms,
            "reason": probe.reason,
        },
        "vision_model": settings.vision_model,
        "logic_primary_model": settings.logic_primary_model,
        "logic_local_model": settings.logic_local_model,
    }


@app.get("/v1/models")
def list_models() -> dict:
    return {"object": "list", "data": [{"id": "brother", "object": "model", "owned_by": "erge"}]}


@app.post("/v1/chat/completions", response_model=None)
def chat_completions(request: ChatCompletionRequest) -> Any:
    response = router.handle_chat_completion(request)
    if request.stream:
        return StreamingResponse(_iter_openai_stream(response), media_type="text/event-stream")
    return response.model_dump()
