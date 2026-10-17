# Operations Runbook

## Health Checks

```bash
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v6-control.sh status
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v6-control.sh runner
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v6-control.sh runner-start
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v6-control.sh queue
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v6-control.sh diagnose
```

## Safe Recovery

Use this when queue/state is stuck:

```bash
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v6-control.sh reset
```

This clears queue + state and restarts the runner.

Use this when replies start reviving stale jokes or old chat context:

```bash
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v6-control.sh memory-clear-all
```

This clears short-term contact summaries and recent events while preserving long-term contact profiles.

## Log Sources

- Runtime events: `runtime/events.jsonl`
- Fallback runtime events: `~/.openclaw/logs/wechat-autoreply-events-fallback.jsonl`
- Runner stdout: `~/.openclaw/logs/wechat-autoreply-v6.log`
- Runner stderr: `~/.openclaw/logs/wechat-autoreply-v6.err.log`

## Debug Checklist

1. Confirm enabled + queue state (`status`, `queue`).
2. Verify `menu_bar_checked` signal in `events.jsonl`.
3. Track queue transitions:
   - `draft_saved_locally`
   - `pending_refreshed_latest`
   - `auto_sent`
   - `pending_cancelled`
4. If stuck, `reset` and retest with one whitelist contact.

## Verification

```bash
cd /Users/shawnwang/Documents/Playground
./wechat_env/bin/python -m unittest discover -s tests -v
./wechat_env/bin/python selftest.py
./wechat_env/bin/python -m py_compile wechat_autoreply/*.py
```
