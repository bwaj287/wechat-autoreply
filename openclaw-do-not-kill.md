# OpenClaw Do Not Kill

If you want OpenClaw to keep working, do not kill these:

1. `openclaw-gateway`
   This is the main OpenClaw gateway service.
   If you kill it, Control UI / Gateway-routed agents stop responding until launchd restarts it.
   How to identify it:
   `launchctl` label: `ai.openclaw.gateway`
   process name: `openclaw-gateway`

2. `wechat-autoreply-v1 [DO NOT KILL]`
   This is the Terminal-hosted WeChat auto-reply V1 runner.
   If you kill it, WeChat auto-reply pauses until the launcher recreates it.
   How to identify it:
   Terminal tab title: `wechat-autoreply-v1 [DO NOT KILL]`
   child command: `/Users/shawnwang/.openclaw/workspace/scripts/wechat-autoreply-v1-terminal-host.sh`

These are not as critical for the main chat path:

- `ai.openclaw.idle.model.switch`
- `ai.openclaw.daily.discord.health`
- `ai.openclaw.daily.ios.widget.health`
- `ai.openclaw.ios.widget.heartbeat`
- `ai.openclaw.keep.mac.awake`
