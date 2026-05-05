# WeChat Auto-Reply (OpenClaw)

This repository contains a local macOS WeChat auto-reply agent built with UI automation + OCR.

It does not use any official WeChat API.  
All behaviors are executed by visual detection, window control, keyboard simulation, and a local LLM.

This branch also includes the current image-aware reply path:

- plain text messages still use the normal OCR + context flow
- image / sticker / photo-like messages can be routed through `brother` (the local multimodal gateway)
- visual debug crops are preserved under `runtime/captures/` for later inspection

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
  erge_client.py             # Brother multimodal client (image-aware reply path)
  orchestrator.py            # Core state machine
  wechat_ui.py               # WeChat probing, OCR extraction, UI actions
  vision.py                  # Menu signal detection (icon + digit context)
  idle.py                    # System idle detection via Quartz
  state_store.py             # Runtime state persistence
  config_store.py            # Config read/write and toggles
erge_gateway/
  server.py                  # OpenAI-compatible Brother gateway
  router.py                  # Attachment / vision / logic routing
  clients/                   # Vision + logic backend clients
  preprocess/                # PDF / DOCX / XLSX ingest helpers
runtime/
  config.json                # Runtime config
  state.json                 # Runtime state snapshot
  contact_memory.json        # Per-contact long-term profile + short-term compressed memory
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

## Brother Image-Aware Reply Path

The current image-aware path is designed to be additive and low-risk:

- text messages stay on the normal OCR-first flow
- image-like messages can use the `brother` gateway when enabled and healthy
- if `brother` is unhealthy, the system falls back to the local small model

Current default routing:

- `brother` health endpoint: `http://127.0.0.1:4010/health`
- `brother` chat endpoint: `http://127.0.0.1:4010/v1/chat/completions`
- model alias: `brother`

### How Image Routing Works

When a reply generation call includes a WeChat screenshot:

1. The runner chooses a preferred chat screenshot from the current probe.
2. `wechat_autoreply/erge_client.py` builds a visual focus image.
3. If the inbound looks like a photo / picture / sticker placeholder:
   - it crops toward the newest incoming media area
   - attempts to isolate the likely media block
   - upscales the crop before sending it to `brother`
4. If the inbound is normal text:
   - it still crops away the left roster sidebar
   - keeps only the right chat panel as supporting evidence

This is intentionally conservative:

- normal text should not accidentally fall into the image path
- image routing is an enhancement, not a replacement for text handling

### Debug Images Saved To `runtime/captures`

The system preserves generated debug crops so we can inspect what `brother` actually saw.

Common file patterns:

- `*-chat-focus-*.png`
  - right chat panel only
- `*-vision-focus-*.png`
  - generic image-oriented focus crop
- `*-vision-media-focus-*.png`
  - media block isolated and upscaled for image-heavy replies

These captures are useful for debugging:

- image message not understood
- wrong reply grounded in the wrong part of the UI
- text vs image routing mistakes
- OCR jitter around mixed image + caption messages

Cleanup keeps only recent captures; older artifacts are pruned by the capture cleanup flow.

## Gateway Command Reference

All control commands go through:

```bash
./wechat_env/bin/python gateway_control.py <command>
```

Supported commands:

- `on` -> enable auto-reply runner.
- `off` -> disable claim/send execution.
- `status` -> show switch + recent trace lines.
- `runner` -> show runner/host process health.
- `runner-start` -> start runner immediately if offline.
- `queue` -> show pending queue.
- `since` -> show how many auto replies were sent since the last `since` check.
- `diagnose` -> detailed diagnostics and recent events.
- `reset` -> clear runtime state and restart cleanly.
- `restart` -> same behavior as reset.
- `style-show` -> show current reply style instructions.
- `style-set "<text>"` -> update style instructions.
- `memory-show <contact>` -> inspect that contact's long-term profile + short-term memory.
- `memory-set <contact> "<profile>"` -> manually set that contact's long-term profile.
- `memory-clear <contact>` -> clear short-term memory drift while keeping the profile.
- `memory-lock <contact>` -> lock that contact's long-term profile.
- `memory-unlock <contact>` -> unlock that contact's long-term profile.
- `command` or `/command` -> show command help.

Examples:

```bash
./wechat_env/bin/python gateway_control.py status
./wechat_env/bin/python gateway_control.py runner
./wechat_env/bin/python gateway_control.py runner-start
./wechat_env/bin/python gateway_control.py queue
./wechat_env/bin/python gateway_control.py since
./wechat_env/bin/python gateway_control.py diagnose
./wechat_env/bin/python gateway_control.py style-set "Natural, short, conversational, no sentence-final periods"
./wechat_env/bin/python gateway_control.py memory-show May
./wechat_env/bin/python gateway_control.py memory-set May "Close friend, casual tone, can tease lightly, avoid sounding oily"
./wechat_env/bin/python gateway_control.py memory-set "一条正直的咸鱼" "Normal friend tone, stay natural, do not oversell familiarity"
```

## Per-Contact Memory Model

The current reply stack now has three separate layers:

1. Global style rules
   - Stored in `runtime/config.json`
   - Controls how Shawn generally sounds on WeChat
   - Updated with `style-set`

2. Long-term contact profile
   - Stored per contact in `runtime/contact_memory.json`
   - Intended for stable traits:
     - relationship
     - preferred tone
     - common topics
     - boundaries / things to avoid
   - Updated manually with `memory-set`

3. Short-term compressed memory
   - Also stored per contact in `runtime/contact_memory.json`
   - Automatically refreshed from recent valid chats and successful replies
   - Used to preserve continuity without stuffing the full history into prompt context

### Why the Memory Is Split This Way

This separation is deliberate.

We do **not** want:

- OCR mistakes to become permanent facts
- one accidental conversation to define a person's long-term profile
- the system to "self-train" into a distorted persona over time

So the rule is:

- automatic logic may update **short-term compressed memory**
- automatic logic may **not** rewrite the **long-term profile**

Long-term profile is treated as a human-owned control surface.

### Drift Safeguards

To reduce memory drift:

- long-term profile is manual-first
- short-term memory has retention limits and event caps
- repeated duplicate fragments are de-duplicated
- obvious OCR noise is filtered before memory write
- `memory-clear <contact>` lets you wipe short-term drift without deleting the stable profile
- `memory-lock <contact>` lets you freeze the profile deliberately

### What Gets Injected Into Reply Generation

Before generating a draft, the prompt now contains:

- global style instructions
- `Contact profile: ...`
- `Longer-term memory with this contact: ...`
- recent chat context
- latest inbound message

This applies to:

- local small-model generation
- `brother` / multimodal generation

### Recommended Workflow

Use automatic short-term memory for continuity, and only manually author long-term profile when needed.

Examples:

- `May`: "Close friend, casual tone, can tease lightly, avoid sounding too eager"
- `Darren`: "Bro tone, direct, no long explanations"
- `Ted Liu`: "Friendly but normal, do not sound dismissive"

If a reply starts feeling "off" because of recent OCR or transient context, do:

```bash
./wechat_env/bin/python gateway_control.py memory-clear May
```

If a profile feels good and you do not want it accidentally changed later:

```bash
./wechat_env/bin/python gateway_control.py memory-lock May
```

### Default Long-Term Profiles

This branch also supports seeding a baseline long-term profile for each whitelist contact.

The intended use is:

- write a safe default once
- lock it
- let the system only evolve the short-term summary

Example patterns:

- family -> warmer, steadier, less teasing
- customer / work contact -> clearer and slightly more polite
- close friends -> more casual, more playful, more shorthand
- sibling -> natural and close, but not greasy or overly dramatic

This gives the model a stable relationship frame without letting OCR accidents rewrite identity-level facts.

### Notes About Contacts With Spaces

For local CLI usage, quote contact names with spaces:

```bash
./wechat_env/bin/python gateway_control.py memory-show "Ted Liu"
./wechat_env/bin/python gateway_control.py memory-set "一条正直的咸鱼" "Normal friend tone, stay natural, lightly playful"
```

For OpenClaw chat commands, either quote the contact name or use the `::` separator:

```text
memory-show "Ted Liu"
memory-set "一条正直的咸鱼" Normal friend tone, stay natural, lightly playful
memory-set 一条正直的咸鱼 :: Normal friend tone, stay natural, lightly playful
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
   - Whitelist contact: open chat, extract inbound + recent context, draft reply, enqueue pending.
   - Non-whitelist contact: open row to clear unread only.
4. Hide WeChat and restore previous front app.

For image-like inbound messages:

- the claim step may preserve placeholder text such as `表情包` or `[Photo]`
- if a richer screenshot path is available, reply generation can use visual evidence through `brother`
- this means image message handling currently depends on both OCR text and the saved chat screenshot

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
- Pending recheck now suppresses some short tail-fragment regressions (for example when a long sentence is re-read as only the last few characters).

Image-path specific reliability notes:

- WeChat roster screenshots are cropped to remove the left contact list before multimodal analysis.
- Photo / sticker-like messages can trigger a media-focused crop instead of sending the full UI image.
- Media-focused crops are upscaled before being sent to `brother`.
- The current system is better at "image classification + coarse semantic grounding" than exact OCR from inside images.
- If image meaning is unclear, the model should answer conservatively rather than hallucinate details.

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
- `reply_context_messages`: number of recent chat lines provided to LLM context window.
- `reply_style_instructions`: reply tone instructions.
- `emoji_pack_zip_path`: emoji pack zip path.
- `reply_emoji_enabled`, `reply_emoji_min_count`, `reply_emoji_max_count`: emoji policy.
- `erge_enabled`: enable multimodal brother routing.
- `erge_model`: model alias used by the brother gateway.
- `erge_gateway_url`: multimodal generation endpoint.
- `erge_health_url`: health check endpoint for brother availability.
- `erge_health_timeout_seconds`, `erge_health_cache_seconds`: brother health probing controls.
- `erge_request_timeout_seconds`: brother request timeout.

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
