# Operations Runbook

## Health Checks

```bash
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v1-control.sh status
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v1-control.sh queue
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v1-control.sh diagnose
```

## Safe Recovery

Use this when queue/state is stuck:

```bash
/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v1-control.sh reset
```

This clears queue + state and restarts the runner.

## Log Sources

- Runtime events: `runtime/events.jsonl`
- Runner stdout: `~/.openclaw/logs/wechat-autoreply-v1.log`
- Runner stderr: `~/.openclaw/logs/wechat-autoreply-v1.err.log`

## Debug Checklist

1. Confirm enabled + queue state (`status`, `queue`).
2. Verify `menu_bar_checked` signal in `events.jsonl`.
3. Track queue transitions:
   - `draft_saved_locally`
   - `pending_refreshed_latest`
   - `auto_sent`
   - `pending_cancelled`
4. If stuck, `reset` and retest with one whitelist contact.
