# Architecture

## Goals

- Keep runtime behavior deterministic and observable.
- Make on-call debugging possible from logs + queue state only.
- Enable safe feature extension without breaking gateway controls.

## Layers

### 1. Entrypoints

- `apps/runner/cli.py`: long-running tick loop.
- `apps/gateway/cli.py`: operator commands (`on/off/status/queue/reset/restart`).

Legacy wrappers:

- `main.py`
- `gateway_control.py`

### 2. Core Package

- `wechat_autoreply/orchestrator.py`: state machine + decision flow.
- `wechat_autoreply/wechat_ui.py`: WeChat UI automation and OCR extraction.
- `wechat_autoreply/vision.py`: menubar unread signal detection.
- `wechat_autoreply/idle.py`: macOS idle detection.
- `wechat_autoreply/ollama_client.py`: LLM reply generation.

### 3. Persistence & Observability

- `runtime/config.json`: mutable runtime config.
- `runtime/state.json`: queue + control-plane state.
- `runtime/events.jsonl`: append-only event trace.

### 4. Native Helpers

- `tools/wechat_row_badges.swift`: chat-row unread badge detection.
- `tools/wechat_bubble_roles.swift`: inbound/outbound bubble role classification.
- `tools/wechat_ocr.swift`: OCR bridge.

## Extension Points

1. New claim/send policy:
   - Add pure helper logic near orchestrator helper methods.
   - Keep queue mutation in one place (`sync_pending_state` path).
2. New UI detection:
   - Add detector in `tools/` + call from `wechat_ui.py`.
   - Keep thresholds normalized to window-relative coordinates.
3. New control command:
   - Implement in `apps/gateway/cli.py`.
   - Keep output one-line friendly for Gateway display.
