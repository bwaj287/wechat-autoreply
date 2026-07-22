# Architecture

## Goals

- Keep runtime behavior deterministic and observable.
- Treat OCR, badge, and bubble-role output as evidence rather than truth.
- Keep policy decisions pure and independently testable.
- Keep UI side effects behind the orchestrator boundary.
- Preserve a restartable FIFO queue with one mutation path.

## Runtime Flow

1. `AutoReplyRunner.tick()` samples idle time and the Dock/menu unread signal.
2. `claim_policy` decides whether evidence is strong enough to enter claim flow.
3. Passive recovery first captures the hidden roster; WeChat is focused only after a whitelist numeric badge is found.
4. `wechat_ui` captures windows and returns OCR observations, badge evidence, and panel bubbles.
5. Claim flow resolves inbound text, suppresses self-echoes, and appends a draft to the FIFO queue.
6. Pending flow waits for the delay, collects multi-frame OCR samples, and asks `recheck_policy` for consensus.
7. Manual-reply evidence is classified by `manual_reply_policy` before cancellation.
8. Send is verified against a committed outbound bubble; uncertain sends remain queued for bounded retry.

## Modules

### Entrypoints

- `apps/runner/cli.py`: single-process tick loop and runner lock.
- `apps/gateway/cli.py`: operational commands.

### Orchestration

- `orchestrator.py`: coordinates sensors, policies, persistence, model calls, and UI side effects.
- `claim_policy.py`: contact matching, numeric badge thresholds, badge streaks, passive preflight, and claim scheduling.
- `badge_detection.py`: pixel-level red/digit evidence extraction, including attached warm-avatar badges.
- `pending_queue.py`: FIFO queue synchronization, lookup, removal, and stale-item pruning.
- `recheck_policy.py`: deterministic voting over send-time OCR samples.
- `manual_reply_policy.py`: classifies evidence that the user replied manually.
- `outbound_history.py`: canonical send matching and bounded recent-outbound self-echo memory.

### Observation

- `wechat_ui.py`: WeChat window capture, OCR preparation, row extraction, bubble extraction, and UI actions.
- `vision.py`: Dock/menu unread signal detection.
- `ocr.py`: native Vision helper adapter.
- `json_output.py`: first-complete-JSON parsing for noisy native output.

### Persistence And Observability

- `state_store.py`: atomic state persistence at `~/.openclaw/workspace/wechat-auto-reply-state.json`.
- `event_log.py`: serialized event writes, retry, and fallback logging.
- `capture_cleanup.py`: retention cleanup isolated from the main state machine.

## Invariants

- A plain red avatar is never actionable unread evidence.
- Passive scans do not focus WeChat unless a whitelist numeric badge is found.
- A global Dock/menu unread signal without a visible whitelist numeric badge is treated as non-whitelist; the runner must not search through whitelist contacts.
- A gray latest inbound bubble can override text equality with the previous outbound.
- A pending item is identified and removed by inbound fingerprint, not contact name.
- `pending` is only a compatibility mirror of `pending_queue[0]`.
- Event logging and retention cleanup cannot terminate the reply state machine.
- Foreground WeChat sessions always emit paired open/hide events and restore the previous app when possible.

## Verification

- `tests/`: fast policy and persistence tests.
- `selftest.py`: integration-style runner scenarios with fake sensors/UI.
- Native image helpers remain covered by synthetic image regression paths in `selftest.py`.
