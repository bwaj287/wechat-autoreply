# OpenClaw Do Not Kill

If you want OpenClaw to keep working, do not kill these:

1. `openclaw-gateway`
   This is the main OpenClaw gateway service.
   If you kill it, Control UI / Gateway-routed agents stop responding until launchd restarts it.
   How to identify it:
   `launchctl` label: `ai.openclaw.gateway`
   process name: `openclaw-gateway`

2. `ai.openclaw.wechat.autoreply.v6`
   This is the launchd-managed WeChat auto-reply V6 runner.
   If you kill its Python process, launchd restarts it automatically.
   How to identify it:
   `launchctl` label: `ai.openclaw.wechat.autoreply.v6`
   child command: `/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v6-run.sh`

These are not as critical for the main chat path:

- `ai.openclaw.idle.model.switch`
- `ai.openclaw.daily.discord.health`
- `ai.openclaw.daily.ios.widget.health`
- `ai.openclaw.ios.widget.heartbeat`
- `ai.openclaw.keep.mac.awake`
