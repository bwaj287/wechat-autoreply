# WeChat Auto-Reply (OpenClaw)

Production-style local WeChat auto-reply agent for macOS.

## Repository Structure

```text
apps/
  gateway/         # Gateway CLI entrypoint and control commands
  runner/          # Runner CLI entrypoint
docs/
  ARCHITECTURE.md  # System boundaries, data flow, extension points
  OPERATIONS.md    # Runbook and maintenance SOP
tools/             # Swift helpers for OCR, badge detection, bubble roles
wechat_autoreply/  # Core package (orchestration, sensors, state, UI automation)
runtime/           # Runtime state, events, captures (ignored by git)
```

## Compatibility Entry Points

These legacy paths are intentionally kept for existing scripts:

- `main.py` -> delegates to `apps.runner.cli`
- `gateway_control.py` -> delegates to `apps.gateway.cli`

## Common Commands

```bash
./wechat_env/bin/python selftest.py
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v1-control.sh status
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v1-control.sh queue
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v1-control.sh reset
```

## Feature Development Workflow

1. Add new behavior in `wechat_autoreply/` first (domain logic + orchestrator path).
2. Add or update self-tests in `selftest.py`.
3. Validate with `./wechat_env/bin/python selftest.py`.
4. Keep gateway output stable for operators (`status`, `queue`, `reset`).
