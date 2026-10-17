# V8 Mac Trial

V8 is based on `v7-stable-20261017`. The first change consolidates three identical atomic JSON writers and two identical message-filtering functions. It retains validation, message roles, OCR filtering, queue behavior, manual-reply protection, duplicate suppression, model routing, and persistence formats.

Validation: 46 unit tests and the complete fake-UI selftest pass. Tests include JSON formatting and replacement, preserving existing state on serialization failure, and independent inbound/outbound filtering with ordering and deep copies.

## Runtime layout

- Stable V7 code: `/Users/shawnwang/.openclaw/runtime/wechat-autoreply-v7-app`.
- V8 trial code: `/Users/shawnwang/.openclaw/runtime/wechat-autoreply-v8-app`.
- Python: `/Users/shawnwang/.openclaw/runtime/wechat-autoreply-v6-venv/bin/python`.
- Single service: `ai.openclaw.wechat.autoreply.v6`.

The V8 `runtime/`, allowlist, and switch link to V7's existing mutable data. Both versions therefore use the same lock, configuration, events, and contact memory. The authoritative queue and recent-send history remain at `~/.openclaw/workspace/wechat-auto-reply-state.json`. V7 application code is not modified.

Do not launch a second runner for testing. The installed selector reloads the existing job and updates both the run and control wrappers to the selected application directory.

## Select V8

```bash
~/.openclaw/workspace/scripts/wechat-autoreply-runtime-select.sh v8
```

## Roll back to V7

```bash
~/.openclaw/workspace/scripts/wechat-autoreply-runtime-select.sh v7
```

Keep the latest shared runtime data when rolling back. Never restore an old queue snapshot or use `reset` merely to switch versions: either can lose pending work or re-enable duplicate sends.

## Inspect the active runner

```bash
~/.openclaw/workspace/scripts/wechat-autoreply-v6-control.sh runner
~/.openclaw/workspace/scripts/wechat-autoreply-v6-control.sh status
```

Compare code with the stable tag using `git diff v7-stable-20261017..feature/v8 -- wechat_autoreply tests`. Unit tests use fake UI or pure helpers; successful startup and healthy ticks do not establish that every real conversation has been exercised.
