# WeChat Auto-Reply (OpenClaw)

This repository contains a local macOS WeChat auto-reply agent built with UI automation + OCR.

It does not use any official WeChat API.  
All behaviors are executed by visual detection, window control, keyboard simulation, and a local LLM.

## Why This Project Exists

The main goal is reliable delayed auto-reply with strong guardrails:

- Trigger only when there is a clear unread signal.
- Never hijack keyboard/mouse while the user is active.
- Keep a visible FIFO pending queue.
- Cancel safely when manual reply is detected.
- Expose everything with Gateway commands + event logs.

## Scope and Safety Model

What it does:

- Watches for unread signals.
- Claims whitelist inbound messages.
- Generates draft replies.
- Sends after delay when safe.

What it does not do:

- No official messaging API integration.
- No silent background processing without macOS permissions.
- No forced send when manual activity is detected.

Safety-first principles:

- `idle >= 30s` is required before any UI automation opens WeChat.
- Sending path includes re-check before paste/send.
- Pending items are cancellable on multiple safety signals.
- Runtime state is restartable and inspectable.

## Repository Layout

```text
apps/
  gateway/                   # Gateway command interface
  runner/                    # Runner entry (main loop / one-shot)
docs/
  ARCHITECTURE.md
  OPERATIONS.md
tools/
  wechat_row_badges.swift    # Red unread-dot detection in list rows
  wechat_bubble_roles.swift  # Inbound vs outbound bubble role helper
wechat_autoreply/
  orchestrator.py            # Core state machine
  wechat_ui.py               # WeChat probing, OCR extraction, UI actions
  vision.py                  # Menu signal detection (icon + digit context)
  idle.py                    # System idle detection via Quartz
  state_store.py             # Runtime state persistence
  config_store.py            # Config read/write and toggles
runtime/
  config.json                # Runtime config
  state.json                 # Runtime state snapshot
  events.jsonl               # Append-only event timeline
  captures/                  # Debug captures
```

Compatibility entrypoints:

- `main.py` -> `apps.runner.cli`
- `gateway_control.py` -> `apps.gateway.cli`

## Prerequisites

- macOS (Apple Silicon supported).
- WeChat desktop app installed and signed in.
- Python 3.10+ (tested with local `wechat_env` venv).
- Local Ollama service running.
- Terminal permissions:
  - Accessibility
  - Screen Recording

## Quick Start

### 1) Environment

```bash
cd /Users/shawnwang/Documents/Playground
python3 -m venv wechat_env
./wechat_env/bin/pip install -r requirements.txt
```

### 2) Configure Runtime

Runtime config file:

- `/Users/shawnwang/Documents/Playground/runtime/config.json`

Whitelist and switch files used by operational flow:

- `/Users/shawnwang/Documents/Playground/wechat-whitelist.txt`
- `/Users/shawnwang/Documents/Playground/wechat-auto-reply-switch.txt`

### 3) Start Runner

Long-running loop:

```bash
./wechat_env/bin/python main.py
```

One-shot tick (debug):

```bash
./wechat_env/bin/python apps/runner/cli.py --once --json
```

Dry-run one-shot (no real paste/send):

```bash
./wechat_env/bin/python apps/runner/cli.py --once --dry-run --json
```

## Gateway Command Reference

All control commands go through:

```bash
./wechat_env/bin/python gateway_control.py <command>
```

Supported commands:

- `on` -> enable auto-reply runner.
- `off` -> disable claim/send execution.
- `status` -> show switch + recent trace lines.
- `queue` -> show pending queue.
- `diagnose` -> detailed diagnostics and recent events.
- `reset` -> clear runtime state and restart cleanly.
- `restart` -> same behavior as reset.
- `style-show` -> show current reply style instructions.
- `style-set "<text>"` -> update style instructions.
- `command` or `/command` -> show command help.

Examples:

```bash
./wechat_env/bin/python gateway_control.py status
./wechat_env/bin/python gateway_control.py queue
./wechat_env/bin/python gateway_control.py diagnose
./wechat_env/bin/python gateway_control.py style-set "Natural, short, conversational, no sentence-final periods"
```

## Core Runtime Logic

### Tick Gate

- Runner polls every `poll_interval_seconds` (default `5`).
- UI actions require idle gate (`idle_threshold_seconds`, default `30`).

### Unread Trigger

- Menu signal check runs on interval (`menubar_check_interval_seconds`, default `15`).
- Signal must be actionable before claim flow starts.

### Allowed WeChat Auto-Open Reasons

Only these are valid:

- `claim_scan` -> unread claim scan path.
- `pending_send_due` -> due-send path.

Any other unexpected open path should be treated as a bug.

### Claim Flow (`claim_scan`)

When triggered:

1. Open WeChat.
2. Scan list rows for unread red-dot candidates.
3. For each unread row:
   - Whitelist contact: open chat, extract inbound, draft reply, enqueue pending.
   - Non-whitelist contact: open row to clear unread only.
4. Hide WeChat and restore previous front app.

### Pending Queue Model

- Queue is FIFO.
- Due time: `send_delay_seconds` (default `180` seconds).
- Message-change debounce: `pending_change_debounce_frames` (default `3`) + `pending_change_min_votes` (default `2`) with similarity guard `pending_change_similarity_threshold` (default `0.9`).
- Stale cleanup: `pending_stale_ttl_seconds` (default `86400` seconds).
- Queue state is persisted under runtime state.

### Due-Send Flow (`pending_send_due`)

When queue head is due and idle gate passes:

1. Open WeChat and select target contact.
2. Re-read chat panel for latest inbound/outbound.
3. Run cancellation checks.
4. If safe, focus input, paste draft, send.
5. Verify send result; retry if needed.
6. Hide WeChat and restore foreground app.

## Cancellation Rules

A pending item is cancelled when any of the following is detected:

- Manual reply already exists (`manual_reply_detected`).
- Input box has manual content (`input_box_modified`).
- Inbound becomes empty or invalid during recheck.
- Message fingerprint changes and flow requires refresh/cancel.
- Probe/read failures exceed retry policy.

## OCR and UI Reliability Notes

The system includes protections against OCR jitter and UI ambiguity:

- Row red-dot detection uses constrained ROI + morphology thresholding.
- Row red-dot detection is resolution/HDR adaptive (dynamic scan window + strict/relaxed color passes).
- Row unread now requires numeric badge evidence near avatar (red badge + white digit strokes), not just red pixels.
- Bubble role helper distinguishes inbound/outbound by visual structure.
- Inbound text is normalized and fingerprinted.
- Recheck voting path (`recheck_vote_frames`) stabilizes noisy reads.
- Empty panel path triggers reselect attempt before cancel.
- Dock badge OCR uses dynamic upscaling + adaptive threshold offset for display-scale changes.

Recent hardening included input-box probe sentinel behavior:

- Clipboard sentinel is written before `Cmd+A/C`.
- If copy fails and sentinel remains, it is treated as empty input.
- This avoids stale clipboard false positives for `input_box_modified`.

## Config Reference

Primary runtime keys in `runtime/config.json`:

- `enabled`: master switch.
- `idle_threshold_seconds`: minimum idle before UI automation.
- `send_delay_seconds`: draft delay before send.
- `pending_refresh_delay_seconds`: delay when message changed.
- `send_verify_retry_seconds`: delay before send verification retry.
- `send_max_attempts`: retry budget for unconfirmed sends.
- `menubar_check_interval_seconds`: unread signal sampling interval.
- `pending_stale_ttl_seconds`: stale pending GC TTL.
- `allowed_contacts`: whitelist contacts.
- `ollama_url`, `ollama_model`: local LLM endpoint/model.
- `reply_style_instructions`: reply tone instructions.
- `emoji_pack_zip_path`: emoji pack zip path.
- `reply_emoji_enabled`, `reply_emoji_min_count`, `reply_emoji_max_count`: emoji policy.

## Runtime Files and Meanings

- `runtime/config.json`: mutable runtime config.
- `runtime/state.json`: latest state snapshot.
- `runtime/events.jsonl`: event timeline for diagnostics.
- `runtime/captures/`: OCR/debug images for investigation.
- `runtime/runner.lock`: single-runner process lock.

State file path used in OpenClaw workflow:

- `/Users/shawnwang/.openclaw/workspace/wechat-auto-reply-state.json`

## Event Log Cheatsheet

High-signal events:

- `menu_bar_checked`
- `wechat_window_action` (`reason=claim_scan|pending_send_due`)
- `claim_candidates`
- `draft_saved_locally`
- `pending_recheck_voted`
- `pending_message_changed_recheck`
- `auto_sent`
- `pending_cancelled`
- `claim_logic_bug`

Typical successful sequence:

1. `menu_bar_checked signal=<n>`
2. `wechat_window_action reason=claim_scan`
3. `claim_candidates`
4. `draft_saved_locally`
5. `wechat_window_action reason=pending_send_due`
6. `pending_recheck_voted`
7. `auto_sent`

## Troubleshooting

### WeChat opens but does not send

Check `diagnose` and `events.jsonl` for:

- `pending_cancelled reason=input_box_modified`
- `pending_cancelled reason=manual_reply_detected`
- `pending_message_changed_recheck`

### Queue is not empty but nothing sends

Confirm:

- `enabled` is true.
- Idle gate is actually satisfied.
- No repeated cancellation events are firing.
- WeChat panel selection is correct for the contact.

### Unread exists but claim does not trigger

Check:

- Menu signal sampling events (`menu_bar_checked`).
- macOS permissions (Screen Recording, Accessibility).
- Current display/scale changes that may impact OCR.

### Repeated empty scans

Investigate:

- `pending_reselect_empty_panel` and its result.
- Unexpected UI layout changes (window size, split chat windows).

## Maintenance Workflow

Recommended operational loop:

1. Before testing, run `restart`.
2. Trigger a known whitelist message.
3. Inspect `queue`.
4. Wait due time and inspect `diagnose`.
5. Verify `auto_sent` or explicit cancellation reason.

Useful checks:

```bash
./wechat_env/bin/python -m py_compile wechat_autoreply/orchestrator.py wechat_autoreply/wechat_ui.py
./wechat_env/bin/python gateway_control.py diagnose
```

## Development and Git Notes

- Keep switch/whitelist local files out of commits when they include private data.
- Prefer committing deterministic logic changes and docs separately.
- For release snapshots, push both feature branch and release branch as needed.

## Legal and Privacy Reminder

This project automates personal messaging behavior.  
Use responsibly, comply with local laws/platform policies, and avoid sending sensitive data through logs or prompts.
