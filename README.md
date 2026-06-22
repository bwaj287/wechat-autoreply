# WeChat Auto-Reply V6

运行在 macOS 上的本地微信自动回复系统。项目不使用微信官方 API，而是通过屏幕识别、OCR、窗口控制和键盘模拟完成消息检测、草稿生成与延迟发送。

当前 V6 的重点不是增加更多自动化动作，而是让每一次“打开微信、认领消息、取消草稿、发送回复”都有明确证据、可测试规则和可追踪事件。

> 本项目会操作真实微信窗口并发送真实消息。首次使用请保持自动回复关闭，先完成权限、白名单和 dry-run 验证。

## 当前状态

- 当前代码代际：V6
- 开发分支：`feature/v6_iteration`
- 运行平台：macOS，Apple Silicon
- 主进程：launchd 托管的单 Python runner
- 默认发送延迟：180 秒
- 默认本机模型：`qwen3.5:9b`
- 可选主模型：PC 上的 `erge:27b`，不可达时自动降级到本机模型
- 图片消息：可通过本机 `brother` 多模态网关处理
- launchd 标签：`ai.openclaw.wechat.autoreply.v6`

## V6 解决的问题

V3-V5 运行中出现过的疑难问题已被整理为 V6 的发布约束：

- 没有红点时反复打开微信。
- 暖色或红色头像被误识别为未读 badge。
- Dock 未读信号漏报后完全不触发。
- OCR 把自己刚发的消息当成新消息。
- 对方回复与我方旧消息文本相似时被错误忽略。
- 发送前单帧 OCR 抖动导致误取消或误发送。
- 用户已经手动回复，但队列仍继续发送。
- 同一联系人连续多条消息时删错 pending item。
- 事件日志与清理任务争用文件，导致 runner 因 `Resource deadlock avoided` 退出。
- 原生工具输出附带诊断文本或重复 JSON，导致解析失败。

对应规则和测试入口见 [事故约束](docs/INCIDENT_INVARIANTS.md)。

## 核心安全约束

- 只有明确的数字未读 badge 才能进入消息认领流程，单纯红色像素不算。
- 后台 roster 预检未发现白名单未读时，不把微信切到前台。
- UI 自动化前必须满足系统空闲时间要求，默认 30 秒。
- 草稿进入 FIFO 队列，发送前重新读取聊天面板并进行多帧共识。
- 检测到人工回复、输入框内容或可靠的消息变化时，取消或延后自动发送。
- 每个 pending item 使用入站消息 fingerprint 标识，不按联系人名称粗暴删除。
- 发送结果不确定时保留队列并有限重试，不盲目重复发送。
- 日志或清理失败不能终止回复状态机。
- 微信前台会话结束后尽量恢复之前的前台应用。

## 运行流程

```text
Dock/menu signal ─┐
                  ├─> claim policy ─> hidden roster preflight
periodic sweep ───┘                         │
                                   numeric whitelist badge?
                                      no │        │ yes
                                         │        v
                                      stay hidden  foreground claim
                                                     │
                                             OCR + context + model
                                                     │
                                                FIFO pending
                                                     │
                                           delay + multi-frame recheck
                                                     │
                                      cancel / refresh / verify and send
```

正常情况下，系统只有两个理由可以主动打开微信：

- `claim_scan`：发现可操作的未读证据，读取并认领消息。
- `pending_send_due`：队首草稿到期，执行发送前复检。

其他无理由的前台打开应视为缺陷。

## V6 模块

```text
apps/
  gateway/                     运维命令入口
  runner/                      runner 循环与进程锁

wechat_autoreply/
  orchestrator.py              编排传感器、策略、状态、模型和 UI 副作用
  claim_policy.py              白名单、数字 badge、稳定帧和认领决策
  badge_detection.py           像素级 badge 检测及暖色头像重叠恢复
  pending_queue.py             FIFO 队列及 pending 兼容镜像
  recheck_policy.py            发送前多帧 OCR 共识
  manual_reply_policy.py       人工回复证据分类
  outbound_history.py          发送确认和近期自回声抑制
  wechat_ui.py                 窗口截图、OCR 观察和 UI 动作
  event_log.py                 带锁、重试和降级路径的事件日志
  capture_cleanup.py           截图与事件保留清理
  state_store.py               原子化状态持久化
  erge_client.py               brother 多模态客户端

erge_gateway/
  server.py                    OpenAI-compatible brother gateway
  router.py                    文本、图片和附件路由

tools/
  wechat_ocr.swift             macOS Vision OCR helper
  wechat_row_badges.swift      roster badge helper
  wechat_bubble_roles.swift    入站/出站气泡角色 helper

tests/                         快速策略与并发回归测试
selftest.py                    fake UI 集成场景
```

更完整的边界与不变量见 [架构文档](docs/ARCHITECTURE.md)。

## 环境要求

- macOS，已安装并登录微信桌面版。
- Python 3.10+。
- Ollama，至少已安装本机降级模型。
- macOS 为实际 runner 授予：
  - Accessibility
  - Screen Recording
- 项目依赖见 `requirements.txt`。

初始化：

```bash
cd /Users/shawnwang/Documents/Playground
python3 -m venv wechat_env
./wechat_env/bin/pip install -r requirements.txt
ollama pull qwen3.5:9b
```

如需本机图片理解：

```bash
ollama pull qwen3-vl:4b
```

## 本地配置

以下文件属于机器运行配置，不应在不了解内容时直接覆盖：

| 路径 | 用途 |
| --- | --- |
| `runtime/config.json` | 运行参数、模型端点和回复风格 |
| `wechat-whitelist.txt` | 白名单，一行一个联系人 |
| `wechat-auto-reply-switch.txt` | `on` 或 `off` |
| `runtime/contact_memory.json` | 联系人短期摘要和本地画像 |
| `wechat_autoreply/contact_memory_seed.json` | 可版本化的默认联系人画像 |
| `~/.openclaw/workspace/wechat-auto-reply-state.json` | 权威队列和 runner 状态 |

`runtime/config.json` 中的 `allowed_contacts` 只是兼容字段；实际白名单优先从 `wechat-whitelist.txt` 读取。

建议首次启动前：

1. 将 `wechat-auto-reply-switch.txt` 设置为 `off`。
2. 检查 `wechat-whitelist.txt`，只保留允许自动回复的联系人。
3. 确认微信窗口布局和 macOS 权限。
4. 先运行测试和 dry-run。
5. 再通过 Gateway 执行 `on`。

## 启动方式

### 开发调试

长期前台运行：

```bash
./wechat_env/bin/python main.py
```

单次 tick：

```bash
./wechat_env/bin/python main.py --once --json
```

不执行真实粘贴或发送：

```bash
./wechat_env/bin/python main.py --once --dry-run --json
```

不要直接执行 `apps/runner/cli.py`；项目入口是根目录的 `main.py`。

### launchd 生产运行

本机 launchd job：

```text
label: ai.openclaw.wechat.autoreply.v6
plist: ~/Library/LaunchAgents/ai.openclaw.wechat.autoreply.v6.plist
entry: ~/.openclaw/workspace/scripts/wechat-autoreply-v6-run.sh
cwd:   /Users/shawnwang/Documents/Playground
```

查看和重启：

```bash
launchctl print gui/$(id -u)/ai.openclaw.wechat.autoreply.v6
launchctl kickstart -k gui/$(id -u)/ai.openclaw.wechat.autoreply.v6
```

runner 使用 `runtime/runner.lock` 保证同一时间只有一个实例。服务已经运行时，再执行 `main.py --once` 会正常提示 runner 已存在。

修改 Python 代码后必须重启 launchd job，运行中的旧进程不会自动加载新代码。

## Gateway 运维命令

统一入口：

```bash
./wechat_env/bin/python gateway_control.py <command>
```

常用命令：

| 命令 | 说明 |
| --- | --- |
| `on` / `off` | 开启或关闭自动回复 |
| `status` | 查看开关、队列数量和近期关键事件 |
| `runner` | 查看 runner 进程健康 |
| `runner-start` | runner 离线时启动 |
| `queue` | 查看 FIFO 待发送队列 |
| `since` | 查看自上次查询后自动发送的数量 |
| `diagnose` | 查看详细诊断和近期事件 |
| `restart` / `reset` | 清空 runtime state 并重启 |
| `style-show` | 查看回复风格 |
| `style-set "<规则>"` | 更新回复风格 |
| `memory-show <联系人>` | 查看联系人画像与短期摘要 |
| `memory-set <联系人> "<画像>"` | 设置长期画像 |
| `memory-clear <联系人>` | 清理短期记忆，保留长期画像 |
| `memory-clear-all` | 清理所有联系人短期记忆，保留长期画像 |
| `memory-lock <联系人>` | 锁定长期画像 |
| `memory-unlock <联系人>` | 解锁长期画像 |
| `command` | 显示完整帮助 |

示例：

```bash
./wechat_env/bin/python gateway_control.py status
./wechat_env/bin/python gateway_control.py queue
./wechat_env/bin/python gateway_control.py diagnose
./wechat_env/bin/python gateway_control.py style-set "自然、简短、口语化，不要句号"
./wechat_env/bin/python gateway_control.py memory-show "Ted Liu"
./wechat_env/bin/python gateway_control.py memory-clear-all
```

`restart/reset` 会清空 pending 队列。除非状态确实卡死，不要把它当作普通代码重载命令；仅加载新代码时优先使用 `launchctl kickstart -k`。

## 模型路由

默认回复路由：

```text
runner
  └─ brother gateway: http://127.0.0.1:4010
       ├─ PC logic primary: erge:27b @ http://192.168.10.2:11434
       ├─ local logic fallback: qwen3.5:9b @ http://127.0.0.1:11434
       └─ local vision: qwen3-vl:4b @ http://127.0.0.1:11434
```

Gateway 健康不等于 PC 模型可达。检查：

```bash
curl -s http://127.0.0.1:4010/health
```

重点字段：

- `logic_probe.status=healthy`：PC 主模型可用。
- `logic_probe.reason=pc_unreachable`：实际回复会降级到本机 `qwen3.5:9b`。
- `logic_probe.reason=pc_model_missing`：PC 可达，但不存在配置的模型标签。

模型名称和端点可通过 `ERGE_*` 环境变量覆盖，默认值见 `erge_gateway/config.py`。

文本消息仍以 OCR 和聊天上下文为主。图片、照片或表情包消息可生成聚焦裁剪并交给 `brother`，调试图保存在 `runtime/captures/`：

- `*-chat-focus-*.png`
- `*-vision-focus-*.png`
- `*-vision-media-focus-*.png`

## 关键配置

| 配置项 | 默认值 | 作用 |
| --- | ---: | --- |
| `idle_threshold_seconds` | `30` | UI 自动化前最小空闲时间 |
| `send_delay_seconds` | `180` | 草稿进入队列后的发送延迟 |
| `poll_interval_seconds` | `5` | runner tick 间隔 |
| `menubar_check_interval_seconds` | `15` | Dock/menu 未读检查间隔 |
| `passive_roster_sweep_enabled` | `true` | Dock 信号漏报时启用后台 roster 恢复 |
| `roster_sweep_interval_seconds` | `60` | 后台 roster 预检间隔 |
| `badge_stability_frames` | `2` | 白名单 badge 稳定帧要求 |
| `pending_stale_ttl_seconds` | `86400` | pending 最大保留时间 |
| `recent_auto_outbound_ttl_seconds` | `21600` | 自回声历史保留时间 |
| `send_verify_retry_seconds` | `45` | 未确认发送的重试等待 |
| `send_max_attempts` | `2` | 最大尝试发送次数 |
| `capture_retention_days` | `2` | 调试截图和事件保留天数 |
| `ollama_model` | `qwen3.5:9b` | 本机文本模型 |
| `erge_enabled` | `true` | 是否启用 brother 路由 |
| `reply_context_messages` | `4` | 提供给模型的近期消息数 |
| `contact_memory_max_events` | `6` | 每个联系人短期记忆最多保留事件数 |
| `contact_memory_retention_days` | `3` | 每个联系人短期记忆保留天数 |
| `max_reply_chars` | `90` | 回复长度上限 |

## 状态、日志与截图

| 路径 | 内容 |
| --- | --- |
| `runtime/events.jsonl` | 主要结构化事件流 |
| `~/.openclaw/logs/wechat-autoreply-events-fallback.jsonl` | 主事件文件不可写时的降级日志 |
| `~/.openclaw/logs/wechat-autoreply-v6.log` | launchd stdout |
| `~/.openclaw/logs/wechat-autoreply-v6.err.log` | launchd stderr |
| `runtime/captures/` | roster、聊天面板和模型聚焦截图 |
| `~/.openclaw/workspace/wechat-auto-reply-state.json` | 权威 pending queue 和运行状态 |

高信号事件：

| 事件 | 含义 |
| --- | --- |
| `menu_bar_checked` | Dock/menu 未读采样 |
| `passive_roster_preflight` | 不聚焦微信的后台 roster 预检 |
| `wechat_window_action` | 微信进入或退出前台及原因 |
| `claim_candidates` | 本轮检测到的候选行 |
| `draft_saved_locally` | 草稿已加入队列 |
| `pending_recheck_voted` | 发送前多帧复检结果 |
| `pending_message_changed_recheck` | 消息变化，延后重新判断 |
| `auto_sent` | 已确认发送 |
| `pending_cancelled` | pending 被安全规则取消 |
| `runtime_cleanup_failed` | 清理失败，但 runner 应继续运行 |
| `runner_error` | runner tick 异常 |

典型成功链路：

```text
menu_bar_checked
-> claim_candidates
-> draft_saved_locally
-> pending_recheck_voted
-> auto_sent
```

## 排障

### 有新消息但没有回复

```bash
./wechat_env/bin/python gateway_control.py status
./wechat_env/bin/python gateway_control.py runner
./wechat_env/bin/python gateway_control.py queue
./wechat_env/bin/python gateway_control.py diagnose
tail -n 100 runtime/events.jsonl
```

依次确认：

1. 开关是否为 `on`。
2. runner 是否运行。
3. 联系人是否在 `wechat-whitelist.txt`。
4. `menu_bar_checked` 或 `passive_roster_preflight` 是否发现 badge。
5. 是否生成 `draft_saved_locally`。
6. 是否因系统不够 idle 尚未到发送窗口。
7. 是否出现 `pending_cancelled` 或 `pending_message_changed_recheck`。

### 没有新消息却打开微信

检查最近的 `wechat_window_action`。合法原因只有 `claim_scan` 和 `pending_send_due`。如果后台预检显示 `badge_detected=false` 后仍进入 `claim_scan`，保留对应截图和事件作为回归样本。

### 红色头像被当作未读

检查 `claim_candidates` 和对应 roster 截图。V6 必须同时看到红色 badge 区域和白色数字笔画；单纯暖色头像不应触发。相关代码位于 `badge_detection.py` 和 `claim_policy.py`。

### 队列有内容但迟迟不发

检查：

- 系统 idle 是否达到阈值。
- `due_at` 是否已经到达。
- 是否反复出现 `pending_message_changed_recheck`。
- 是否检测到人工回复或输入框内容。
- 当前聊天面板是否选中了正确联系人。

### 模型回复很慢

先检查 brother 的 `logic_probe`。PC 不可达时会回退到 M4 Mac mini 上的本机模型，速度和回复质量会与 PC 主模型不同。

### 事件日志停止增长

同时检查：

```bash
tail -n 50 runtime/events.jsonl
tail -n 50 ~/.openclaw/logs/wechat-autoreply-events-fallback.jsonl
tail -n 50 ~/.openclaw/logs/wechat-autoreply-v6.err.log
```

V6 对事件写入和保留清理使用同一文件锁，并对 macOS 的 `EAGAIN/EDEADLK` 做短暂重试。即使主日志暂时失败，也不应拖垮 runner。

## 验证

提交或重启服务前运行：

```bash
./wechat_env/bin/python -m unittest discover -s tests -v
./wechat_env/bin/python selftest.py
./wechat_env/bin/python -m py_compile wechat_autoreply/*.py tests/*.py
git diff --check
```

验证真实服务：

```bash
launchctl kickstart -k gui/$(id -u)/ai.openclaw.wechat.autoreply.v6
launchctl print gui/$(id -u)/ai.openclaw.wechat.autoreply.v6 | grep -E 'state =|pid =|runs ='
tail -n 30 runtime/events.jsonl
```

当前 V6 快速测试覆盖：

- 未读 badge 与被动预检策略。
- 暖色头像重叠 badge。
- FIFO pending queue 不变量。
- 发送前 OCR 共识。
- 人工回复证据。
- 发送确认与自回声历史。
- 事件写入、降级日志及并发清理。

## 联系人记忆

回复上下文分为三层：

1. `runtime/config.json` 中的全局语气规则。
2. 每个联系人的人工维护长期画像。
3. 自动压缩、有限保留的短期聊天摘要。

自动逻辑可以更新短期摘要，但不会自行改写长期画像。这样可以避免 OCR 错误或一次偶发聊天永久改变联系人关系判断。

V6 的短期记忆只沉淀最新入站和实际回复；截图里的历史聊天上下文只用于本轮生成，不会被刷新成新的联系人记忆。若发现回复开始复活旧梗、旧场景或明显串上下文，优先执行：

```bash
./wechat_env/bin/python gateway_control.py memory-clear-all
```

联系人包含空格时请加引号：

```bash
./wechat_env/bin/python gateway_control.py memory-show "Ted Liu"
./wechat_env/bin/python gateway_control.py memory-set "Ted Liu" "朋友，正常口语，不要过度热情"
```

## 维护文档

- [Architecture](docs/ARCHITECTURE.md)
- [Operations Runbook](docs/OPERATIONS.md)
- [Incident-Derived Invariants](docs/INCIDENT_INVARIANTS.md)

## 隐私与使用边界

- 白名单、联系人记忆、聊天截图和事件日志可能包含私人信息。
- 提交前检查 `runtime/`、本地白名单、截图和日志是否被 Git 跟踪。
- 不要把真实聊天记录、模型端点凭据或私人联系人信息公开推送。
- 本项目依赖微信桌面 UI，微信升级、显示缩放或窗口布局变化都可能影响识别结果。
- 请遵守当地法律、平台规则和消息接收者的合理预期。
