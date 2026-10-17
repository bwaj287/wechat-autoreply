# WeChat Auto-Reply V8 — Mac Trial

A local WeChat auto-reply system for macOS. It detects messages, generates drafts, and sends delayed replies through screen capture, optical character recognition (OCR), window control, and keyboard simulation. It does not use the official WeChat API.

V8 starts from stable V7 and removes duplicated code without changing reply policies. See the [V7 stable baseline](docs/V7_BASELINE.md) and [V8 Mac trial](docs/V8_TRIAL.md) for validation, deployment, and rollback.

> This application operates a real WeChat window and sends real messages. Keep auto-reply off during initial setup, verify permissions and the allowlist, and run the tests and dry run first.

## Current status

- Stable version: V7.
- Stable development branch: `feature/v7_iteration`.
- Trial branch: `feature/v8`, based on `v7-stable-20261017`.
- Platform: macOS on Apple Silicon.
- Process model: one Python runner managed by launchd.
- Default send delay: 180 seconds.
- Local fallback model: `qwen3.5:9b`.
- Optional primary: a PC-hosted model, with local fallback when unavailable.
- Image messages: supported through the local `brother` multimodal gateway.
- Existing launchd label: `ai.openclaw.wechat.autoreply.v6`. The historical label does not indicate the current code version.

## Recent fixes

### 2026-10-17: V8 simplification trial

- Reuse one atomic JSON writer for configuration, state, and contact memory, retaining the existing file format and replace behavior.
- Reuse one bubble-filtering function for inbound and outbound messages, retaining sender separation, sorting, and independent copies.
- Pass 46 unit tests and the complete fake-UI selftest. No dependencies, reply policies, or state formats were added or changed.
- Keep V7 code in its original directory and use one service and shared runtime data for the V8 trial. See the trial runbook for switching versions.

### 2026-10-17: stable V7 baseline

- Freeze the code currently running on this Mac, based on `1d2f044`.
- Resolve the allowlist and switch relative to the application root, so independent runtime copies use their own control files.
- Validate the baseline with 43 unit tests and the complete fake-UI selftest.
- Keep machine configuration, private runtime data, and unrelated cleanup files outside the release commit.

### 2026-09-24

- Report unavailable WeChat windows in `status`; retry background preflight at the normal scan interval instead of flooding error logs.
- Limit badge scans to the avatar's upper edge, excluding orange/red emoji in contact previews. Real false-positive captures and synthetic badges cover the regression.
- Keep repeatedly duplicated drafts pending for delayed regeneration. Empty drafts never enter the send path.
- Reclaim new unread messages with changed previews or timestamps even when that contact already has a pending item.

### 2026-09-19

- Prevent identical replies to different inbound messages by excluding the previous six hours of automatically sent replies from generation and regenerating duplicates.
- Preserve recent send-safety history during `reset` and `restart`, while clearing pending items.
- Isolate selftest allowlists from this Mac's live allowlist.

### 2026-09-17

- Recover OCR helpers and event logs that macOS cloud storage replaces with `dataless` placeholders. Restore helpers from Git and archive/recreate unavailable event files.
- Preserve explicit sender roles with `[CONTACT_SENT]`, `[YOU_SENT]`, and `[LATEST_CONTACT_SENT]`.
- Exclude unknown sender roles instead of treating them as contact messages.
- Apply the same role constraints to the primary and fallback models. Only the latest contact message is a reply target.
- Add context-ordering, sender-role, unknown-role, and dataless-recovery regressions.
- Clear the affected contacts' short-term automatic memories while preserving their manually maintained, locked profiles.

### 2026-07-23

- Schedule the earliest `due_at` item so a sleeping queue head cannot block other due contacts. Preserve insertion order in storage.
- Update pending items by object or inbound fingerprint, never by an assumed `queue[0]` position.
- Exclude older share-card metadata when newer plain messages follow it.
- Include titles near the panel edge in OCR, normalize `10ck` to the existing `1ock` alias, and avoid reopening an already selected contact.
- Add incident regressions for blocked contacts, non-head updates, share-card contamination, and edge-positioned titles.
- Validate that release with the complete selftest and its then-current 29 unit tests.

## Reliability inherited from V6

Incidents observed in V3–V5 became release requirements retained by V7:

- Repeatedly opening WeChat without an actionable unread badge.
- Mistaking warm/red avatars for badges, or letting avatar pixels dilute a genuine white digit `1`.
- Missing messages when the Dock unread signal fails.
- Losing a message after a click clears its badge but title OCR remains uncertain.
- Reporting a healthy runner while WeChat is on the `Enter Weixin` login screen.
- Treating our own sent messages as new inbound messages or as questions for the model.
- Ignoring genuine contact messages because they resemble a previous outbound reply.
- Cancelling or sending based on unstable single-frame OCR.
- Sending after a manual reply, or removing the wrong pending item.
- Blocking due items behind a sleeping head, or overwriting the head during a non-head retry.
- Combining old share-card content with newer messages.
- Losing an edge-positioned contact title or confusing card text with that title.
- Crashing on event-log/cleanup contention with `Resource deadlock avoided`.
- Failing to parse native output with trailing diagnostics or repeated JSON.

See [incident-derived invariants](docs/INCIDENT_INVARIANTS.md) for the rules and regression entry points.

## Safety invariants

- Only a verified numeric badge can enter the claim flow. Red pixels alone are insufficient, and warm avatar colors cannot override confirmed digit evidence.
- Background roster preflight must find an allowlisted unread message before bringing WeChat forward.
- Global unread activity without a visible numeric allowlist badge is treated as non-allowlisted activity. Clear only visible non-allowlist badges; do not search through allowlisted contacts.
- UI automation requires sufficient system idle time: 30 seconds by default.
- Persist drafts in insertion order, schedule the earliest due item, and re-read the chat with multi-frame consensus before sending.
- Cancel or postpone on reliable manual-reply evidence, input-box content, or changed messages.
- Identify pending items by inbound fingerprint, not contact name or queue position.
- Preserve uncertain sends for bounded verification instead of blindly sending again.
- Preserve message order and sender roles. Never infer an unknown sender as the contact.
- If a click clears a badge, continue only when contact-row evidence, preview text, and the gray inbound bubble agree. Retry uncertainty within a finite budget.
- Report the login screen as `wechat_login_required`, not an empty healthy roster.
- Logging and cleanup failures must not stop the reply state machine.
- Restore the previously active application after a foreground WeChat session whenever possible.

## Runtime flow

```text
Dock/menu signal ─┐
                  ├─> claim policy ─> hidden roster preflight
periodic sweep ───┘                         │
                                   numeric allowlist badge?
                                      no │        │ yes
                                         │        v
                                      stay hidden  foreground claim
                                                     │
                                             OCR + context + model
                                                     │
                                                pending queue
                                                     │
                                      earliest due + multi-frame recheck
                                                     │
                                      cancel / refresh / verify and send
```

Normal operation has two reasons to bring WeChat forward:

- `claim_scan`: read and claim a message after actionable unread evidence.
- `pending_send_due`: recheck the earliest due draft before sending.

Any unexplained foreground opening is a defect.

## Modules

```text
apps/
  gateway/                     Operational command entry point
  runner/                      Runner loop and process lock

wechat_autoreply/
  orchestrator.py              Sensors, policies, state, models, and UI actions
  claim_policy.py              Allowlist, numeric badges, stable frames, claiming
  badge_detection.py           Pixel-level badges and warm-avatar overlap recovery
  pending_queue.py             Pending queue, due scheduling, compatibility mirror
  recheck_policy.py            Multi-frame pre-send OCR consensus
  manual_reply_policy.py       Manual-reply evidence classification
  outbound_history.py          Send confirmation and recent self-echo suppression
  wechat_ui.py                 Window capture, OCR observations, UI actions
  event_log.py                 Locked event logging, retries, fallback
  capture_cleanup.py           Capture and event retention
  state_store.py               Atomic state persistence
  erge_client.py               Brother multimodal client

erge_gateway/
  server.py                    OpenAI-compatible brother gateway
  router.py                    Text, image, and attachment routing

tools/
  wechat_ocr.swift             macOS Vision OCR helper
  wechat_row_badges.swift      Roster badge helper
  wechat_bubble_roles.swift    Inbound/outbound bubble-role helper

tests/                         Fast policy and concurrency regressions
selftest.py                    Fake-UI integration scenarios
```

See [Architecture](docs/ARCHITECTURE.md) for boundaries and invariants.

## Requirements

- macOS with the desktop WeChat application installed and signed in.
- Python 3.10 or later.
- Ollama with at least the local fallback model installed.
- Accessibility and Screen Recording permissions for the actual runner environment.
- Dependencies in `requirements.txt`.

Initialize a development environment:

```bash
cd /Users/shawnwang/Documents/Playground
python3 -m venv wechat_env
./wechat_env/bin/pip install -r requirements.txt
ollama pull qwen3.5:9b
```

For local image understanding:

```bash
ollama pull qwen3-vl:4b
```

## Local configuration

These files hold machine-specific configuration. Inspect them before replacing them:

| Path | Purpose |
| --- | --- |
| `runtime/config.json` | Runtime options, model endpoints, reply style |
| `wechat-whitelist.txt` | Allowlisted contacts, one per line |
| `wechat-auto-reply-switch.txt` | `on` or `off` |
| `runtime/contact_memory.json` | Short-term summaries and local contact profiles |
| `wechat_autoreply/contact_memory_seed.json` | Versioned default profiles |
| `~/.openclaw/workspace/wechat-auto-reply-state.json` | Authoritative queue and runner state |

Paths under `runtime/`, the switch, and the allowlist belong to the current application root. `allowed_contacts` in `runtime/config.json` is a compatibility field; `wechat-whitelist.txt` takes precedence.

Before the first live run:

1. Set `wechat-auto-reply-switch.txt` to `off`.
2. Review `wechat-whitelist.txt` and keep only approved contacts.
3. Verify the WeChat layout and macOS permissions.
4. Run the tests and a dry run.
5. Enable auto-reply through the gateway's `on` command.

## Starting the runner

### Development

Long-running foreground process:

```bash
./wechat_env/bin/python main.py
```

One tick:

```bash
./wechat_env/bin/python main.py --once --json
```

Without real pasting or sending:

```bash
./wechat_env/bin/python main.py --once --dry-run --json
```

Use root-level `main.py`; do not execute `apps/runner/cli.py` directly.

### launchd service on this Mac

```text
label:   ai.openclaw.wechat.autoreply.v6
plist:   ~/Library/LaunchAgents/ai.openclaw.wechat.autoreply.v6.plist
entry:   ~/.openclaw/workspace/scripts/wechat-autoreply-v6-run.sh
V7 root: /Users/shawnwang/.openclaw/runtime/wechat-autoreply-v7-app
```

Inspect or reload the service:

```bash
launchctl print gui/$(id -u)/ai.openclaw.wechat.autoreply.v6
launchctl kickstart -k gui/$(id -u)/ai.openclaw.wechat.autoreply.v6
```

`runtime/runner.lock` prevents two runners in the same application directory. Separate V7/V8 directories have separate locks: never start both against the same WeChat account and shared queue. Use the single existing launchd service when switching versions.

A running process does not reload Python changes automatically. Restart the job after changing the deployed code.

## Gateway operations

Run commands from the same application root as the active runner. The development checkout and deployed copy have separate switches, allowlists, configuration, and event files.

```bash
./wechat_env/bin/python gateway_control.py <command>
```

The examples use the development virtual environment. For a deployed copy, use `/Users/shawnwang/.openclaw/runtime/wechat-autoreply-v6-venv/bin/python` and its `gateway_control.py`.

| Command | Action |
| --- | --- |
| `on` / `off` | Enable or disable auto-reply |
| `status` | Show switch, queue count, recent important events |
| `runner` | Check runner health |
| `runner-start` | Start an offline runner |
| `queue` | Show pending items and due times |
| `since` | Count automatic sends since the last query |
| `diagnose` | Detailed diagnostics and recent events |
| `restart` / `reset` | Clear pending state and restart |
| `style-show` | Show reply style |
| `style-set "<rule>"` | Update reply style |
| `memory-show <contact>` | Show profile and short-term summary |
| `memory-set <contact> "<profile>"` | Set a long-term profile |
| `memory-clear <contact>` | Clear short-term memory, retain profile |
| `memory-clear-all` | Clear all short-term memories, retain profiles |
| `memory-lock <contact>` | Lock a long-term profile |
| `memory-unlock <contact>` | Unlock a long-term profile |
| `command` | Show complete help |

Examples:

```bash
./wechat_env/bin/python gateway_control.py status
./wechat_env/bin/python gateway_control.py queue
./wechat_env/bin/python gateway_control.py diagnose
./wechat_env/bin/python gateway_control.py style-set "Natural, brief, conversational; omit sentence-final periods"
./wechat_env/bin/python gateway_control.py memory-show "Ted Liu"
./wechat_env/bin/python gateway_control.py memory-clear-all
```

`restart` and `reset` clear pending items while preserving recent send-safety history. Use them for stuck state, not routine code reloads. Prefer `launchctl kickstart -k` to reload code without clearing the queue.

## Model routing

```text
runner
  └─ brother gateway: http://127.0.0.1:4010
       ├─ PC logic primary: qwen3.8:27b @ http://192.168.50.83:11434
       ├─ local logic fallback: qwen3.5:9b @ http://127.0.0.1:11434
       └─ local vision: qwen3-vl:4b @ http://127.0.0.1:11434
```

Gateway health alone does not establish primary-model availability:

```bash
curl -s http://127.0.0.1:4010/health
```

Important fields:

- `logic_probe.status=healthy`: PC primary is available.
- `logic_probe.reason=pc_unreachable`: replies fall back to local `qwen3.5:9b`.
- `logic_probe.reason=pc_model_missing`: PC is reachable, but the configured model tag is missing.

The PC communicates with this Mac over the home LAN. Its DHCP-assigned address must be reflected in the gateway configuration if it changes. Game mode is controlled at `http://192.168.50.83:4011/game-mode`; when enabled, new requests fall back locally. A model unloaded after inactivity needs extra time to cold-start.

When the PC is online and game mode is off, the gateway waits for primary inference, including cold start. An offline PC or active game mode falls back immediately. After the PC accepts a request, 90 seconds without response content triggers fallback (`ERGE_PRIMARY_START_TIMEOUT_SECONDS`). A response that has started streaming does not fall back merely because its total duration exceeds 90 seconds.

Override model names and endpoints with `ERGE_*` environment variables. Defaults are in `erge_gateway/config.py`.

Text replies primarily use OCR and chat context. Image/photo/sticker messages can produce focused crops for `brother`; debug images are stored under `runtime/captures/` with `*-chat-focus-*.png`, `*-vision-focus-*.png`, and `*-vision-media-focus-*.png` names.

## Important options

| Option | Default | Purpose |
| --- | ---: | --- |
| `idle_threshold_seconds` | `30` | Minimum idle time before UI automation |
| `send_delay_seconds` | `180` | Delay after a draft is queued |
| `poll_interval_seconds` | `5` | Runner tick interval |
| `menubar_check_interval_seconds` | `15` | Dock/menu sampling interval |
| `passive_roster_sweep_enabled` | `true` | Recover from missed Dock signals |
| `roster_sweep_interval_seconds` | `60` | Background roster preflight interval |
| `badge_stability_frames` | `2` | Required stable numeric badge frames |
| `claim_selection_retry_max_attempts` | `2` | Maximum uncertain-selection claim rounds |
| `claim_selection_retry_ttl_seconds` | `300` | Claim candidate lifetime after a click clears its badge |
| `pending_stale_ttl_seconds` | `86400` | Maximum pending lifetime |
| `recent_auto_outbound_ttl_seconds` | `21600` | Recent send/self-echo history lifetime |
| `send_verify_retry_seconds` | `45` | Delay before verifying an uncertain send |
| `send_confirmation_max_attempts` | `3` | Maximum UI confirmations, including the immediate check |
| `send_confirmation_timeout_seconds` | `180` | Stop reopening and infer sent after a successful action without a visible bubble |
| `send_max_attempts` | `2` | Maximum send actions while the draft remains in the input box |
| `capture_cleanup_interval_seconds` | `43200` | Capture/event cleanup interval: 12 hours |
| `capture_retention_days` | `1` | Capture/event retention in days |
| `ollama_model` | `qwen3.5:9b` | Local text model |
| `erge_enabled` | `true` | Enable brother routing |
| `reply_context_messages` | `4` | Recent messages supplied to the model |
| `contact_memory_max_events` | `6` | Maximum short-term events per contact |
| `contact_memory_retention_days` | `3` | Short-term memory lifetime |
| `max_reply_chars` | `90` | Reply length limit |

## State, logs, and captures

| Path | Contents |
| --- | --- |
| `runtime/events.jsonl` | Primary structured events |
| `~/.openclaw/logs/wechat-autoreply-events-fallback.jsonl` | Fallback when primary event logging fails |
| `~/.openclaw/logs/wechat-autoreply-v6.log` | launchd stdout |
| `~/.openclaw/logs/wechat-autoreply-v6.err.log` | launchd stderr |
| `runtime/captures/` | Roster, chat-panel, model-focus captures |
| `~/.openclaw/workspace/wechat-auto-reply-state.json` | Authoritative pending queue and runner state |

| Event | Meaning |
| --- | --- |
| `menu_bar_checked` | Dock/menu unread sample |
| `passive_roster_preflight` | Roster check without focusing WeChat |
| `wechat_login_required` | Runner alive, WeChat login required |
| `wechat_window_action` | Foreground enter/leave and its reason |
| `claim_candidates` | Candidate rows detected this tick |
| `draft_saved_locally` | Draft queued |
| `pending_recheck_voted` | Multi-frame pre-send result |
| `pending_message_changed_recheck` | Message changed; defer judgment |
| `auto_sent` | Confirmed send |
| `pending_cancelled` | Pending item cancelled by safety rules |
| `runtime_cleanup_failed` | Cleanup failed; runner continues |
| `runner_error` | Tick failed |

Typical successful sequence:

```text
menu_bar_checked
-> claim_candidates
-> draft_saved_locally
-> pending_recheck_voted
-> auto_sent
```

## Troubleshooting

### New message, no reply

```bash
./wechat_env/bin/python gateway_control.py status
./wechat_env/bin/python gateway_control.py runner
./wechat_env/bin/python gateway_control.py queue
./wechat_env/bin/python gateway_control.py diagnose
tail -n 100 runtime/events.jsonl
```

Check, in order: the switch, runner health, login status, allowlist membership, numeric-badge evidence, draft creation, sufficient idle time, and cancellation or message-change events.

### Runner alive, WeChat login required

`status` may display the literal UI warning `微信需要登录`; diagnostics report `last_error: wechat_login_required`. Sign into WeChat. The next successful background preflight clears the warning. Restarting the runner does not bypass login.

### WeChat opens without a new message

Inspect `wechat_window_action`. Normal reasons are `claim_scan` and `pending_send_due`. If `claim_scan` follows `badge_detected=false`, retain the events and capture as a regression sample.

### Red avatar mistaken for unread

Inspect `claim_candidates` and its roster capture. Detection requires badge shape and white digit strokes. Warm avatar color alone is insufficient, and confirmed digits cannot be rejected by the whole row's red-pixel ratio. See `badge_detection.py` and `claim_policy.py`.

### Pending items do not send

Check idle time, the earliest `due_at`, repeated message-change or selection-retry events, manual replies, input-box content, and the selected contact. Sleeping items must not block due contacts; approximate OCR titles should normalize without repeatedly clicking the active chat.

### Slow model replies

Check the brother gateway's `logic_probe`. Local fallback on the M4 Mac mini has different speed and reply quality from the PC primary.

### Event log stops growing

```bash
tail -n 50 runtime/events.jsonl
tail -n 50 ~/.openclaw/logs/wechat-autoreply-events-fallback.jsonl
tail -n 50 ~/.openclaw/logs/wechat-autoreply-v6.err.log
```

Event writes and retention share a file lock and briefly retry macOS `EAGAIN`/`EDEADLK`. Primary logging failures must not stop the runner.

## Validation

Before committing or reloading deployed code:

```bash
./wechat_env/bin/python -m unittest discover -s tests -v
./wechat_env/bin/python selftest.py
./wechat_env/bin/python -m py_compile wechat_autoreply/*.py tests/*.py
git diff --check
```

Check the live service:

```bash
launchctl kickstart -k gui/$(id -u)/ai.openclaw.wechat.autoreply.v6
launchctl print gui/$(id -u)/ai.openclaw.wechat.autoreply.v6 | grep -E 'state =|pid =|runs ='
tail -n 30 runtime/events.jsonl
```

Coverage includes unread/preflight policies, warm-avatar badges, queue scheduling and fingerprints, OCR consensus, manual-reply evidence, send confirmation, recent self-echo history, concurrent event logging/cleanup, context ordering, and known/unknown sender roles.

## Contact memory

Reply context has three layers:

1. Global style rules in `runtime/config.json`.
2. Manually maintained long-term contact profiles.
3. Automatically summarized, bounded short-term chat memory.

Automatic logic updates short-term summaries, never long-term profiles. OCR errors or a one-off conversation must not permanently redefine a relationship.

Only the newest inbound message and the actual reply become fresh short-term memory. Historical screen context is used for the current generation without being recorded again as new memory. If old jokes or unrelated situations keep resurfacing:

```bash
./wechat_env/bin/python gateway_control.py memory-clear-all
```

Clear an individual contact while retaining the long-term profile:

```bash
./wechat_env/bin/python gateway_control.py memory-clear May
./wechat_env/bin/python gateway_control.py memory-clear "Ted Liu"
```

Screen context is ordered by position. `[CONTACT_SENT]` marks contact history, `[YOU_SENT]` marks our history, and `[LATEST_CONTACT_SENT]` is the only reply target. Unknown sender roles are excluded.

Quote contact names containing spaces:

```bash
./wechat_env/bin/python gateway_control.py memory-show "Ted Liu"
./wechat_env/bin/python gateway_control.py memory-set "Ted Liu" "Friend; natural conversation, not overly enthusiastic"
```

## Maintenance documentation

- [V8 Mac Trial](docs/V8_TRIAL.md)
- [V7 Stable Baseline](docs/V7_BASELINE.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Operations Runbook](docs/OPERATIONS.md)
- [Incident-Derived Invariants](docs/INCIDENT_INVARIANTS.md)

## Privacy and operating boundaries

- Allowlists, contact memories, captures, and event logs can contain private information.
- Check that runtime data, local control files, captures, and logs are not tracked before committing.
- Do not publish real conversations, endpoint credentials, or private contact information.
- WeChat updates, display scaling, and window layouts can change recognition behavior.
- Respect applicable laws, platform rules, and recipients' reasonable expectations.
